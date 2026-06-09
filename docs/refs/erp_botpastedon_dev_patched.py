import frappe
import frappe.utils
from frappe.utils import flt, now, getdate, cstr
import json

# ── Helpers ──────────────────────────────────────────────────────────────────

def _validate_api_key(api_key):
	"""Check X-API-Key header against Bot Credential.api_secret. Return bot doc."""
	if not api_key:
		frappe.throw("Missing API key", frappe.AuthenticationError)
	bot = frappe.get_all("Bot Credential",
		filters={"api_secret": api_key, "is_active": 1},
		fields=["name", "bot_id", "channel", "label"],
		limit=1)
	if not bot:
		frappe.throw("Invalid API key", frappe.AuthenticationError)
	# Update last_connected
	frappe.db.set_value("Bot Credential", bot[0].name, "last_connected", now())
	return bot[0]


def _find_channel(platform):
	"""Lookup Channel by name. Platform "Eldorado" / "G2G" → Channel name."""
	ch = frappe.db.get_value("Channel", {"channel_name": platform, "is_active": 1})
	if not ch:
		frappe.throw(f"Channel '{platform}' not found or inactive")
	return ch


def _find_or_create_customer(customer_name):
	"""Fuzzy match Customer by name, create if not exists."""
	if not customer_name:
		return None
	existing = frappe.db.get_value("Customer", {"customer_name": customer_name})
	if existing:
		return existing
	doc = frappe.get_doc({
		"doctype": "Customer",
		"customer_name": customer_name,
		"customer_group": "Individual",
		"territory": "All Territories",
		"customer_type": "Individual",
	})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _find_game_context(game, server):
	"""Lookup Game Context by game title + server. Returns name or None."""
	if not game:
		return None
	# Try exact match first to avoid "Path of Exile" matching "Path of Exile 2"
	title = frappe.db.get_value("Game Title", {"title_name": game, "is_active": 1})
	if not title:
		title = frappe.db.get_value("Game Title", {"title_name": ["like", f"%{game}%"], "is_active": 1})
	if not title:
		return None
	filters = {"game_title": title, "is_active": 1}
	if server:
		filters["server"] = ["like", f"%{server}%"]
	ctx = frappe.db.get_value("Game Context", filters)
	if not ctx and server:
		ctx = frappe.db.get_value("Game Context", {"game_title": title, "is_active": 1})
	return ctx


def _find_currency_item(game_context, item_name):
	"""Lookup Currency Item using Python-side matching to avoid SQL escaping issues.

	Scanner sends: "Boss Materials - Betrayer's Husk", "Custom - Flawless Horadric Gems"
	CI names are: "D4 Betrayer's Husk", "D4 Flawless Horadric Amethyst", etc.
	"""
	if not game_context or not item_name:
		return None
	game_title = None
	if game_context:
		game_title = frappe.db.get_value("Game Context", game_context, "game_title")

	from gege_custom.gege_custom.doctype.currency_item.currency_item import GAME_PREFIXES
	prefix = GAME_PREFIXES.get(game_title) if game_title else None

	filters = {"is_active": 1}
	if game_title:
		filters["game_title"] = game_title
	all_items = frappe.get_all("Currency Item", filters=filters, fields=["name", "item_name"])
	if not all_items:
		return None

	def normalize(s):
		"""Normalize quotes and whitespace for comparison."""
		s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
		return s.lower().strip()

	# Build search candidates from scanner's item_name
	search_parts = [normalize(item_name)]
	if " - " in item_name:
		after_dash = item_name.split(" - ", 1)[-1].strip()
		search_parts.append(normalize(after_dash))

	# Exact match (after normalization)
	for ci in all_items:
		clean = ci.item_name
		if prefix and clean.startswith(f"{prefix} "):
			clean = clean[len(prefix) + 1:]
		for part in search_parts:
			if normalize(clean) == part:
				return ci.name

	# Looser: CI name contained within candidate, or vice versa
	for ci in all_items:
		clean = ci.item_name
		if prefix and clean.startswith(f"{prefix} "):
			clean = clean[len(prefix) + 1:]
		for part in search_parts:
			if normalize(clean) in part or part in normalize(clean):
				return ci.name

	return None


def _get_bot_id_for_channel(channel_name):
	"""Lookup Bot Credential name by channel. Returns first active bot ID or channel_name as fallback."""
	bot = frappe.db.get_value("Bot Credential", {"channel": channel_name, "is_active": 1}, "name")
	return bot or channel_name


def _get_worker_url(channel_name):
	"""Return Worker URL based on channel/platform."""
	# Try Bot Credential first (allow per-bot override)
	bot = frappe.get_all("Bot Credential",
		filters={"channel": channel_name, "is_active": 1},
		fields=["name"], limit=1)
	if bot:
		meta = frappe.get_meta("Bot Credential")
		if meta.has_field("worker_host") and meta.has_field("worker_port"):
			bd = frappe.get_value("Bot Credential", bot[0].name, ["worker_host", "worker_port"], as_dict=True)
			if bd and bd.worker_host:
				port = bd.worker_port or (8001 if "eldo" in channel_name.lower() else 8002)
				return f"http://{bd.worker_host}:{port}/task"

	# Fallback: site_config
	config = frappe.get_site_config()
	if "eldo" in channel_name.lower():
		host = config.get("eldo_worker_host", "localhost")
		port = config.get("eldo_worker_port", 8001)
	else:
		host = config.get("g2g_worker_host", "localhost")
		port = config.get("g2g_worker_port", 8002)
	return f"http://{host}:{port}/task"


def _log_ws_activity(bot_id, action, status, detail, sell_order=None, payload=None):
	"""Create WS Activity Log entry."""
	try:
		frappe.get_doc({
			"doctype": "WS Activity Log",
			"bot_id": bot_id,
			"action": action,
			"status": status,
			"detail": detail,
			"reference_sell_order": sell_order,
			"payload": json.dumps(payload, ensure_ascii=False) if payload else None,
		}).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(f"WS Activity Log error: {action}")


def _extract_qty_from_notes(notes):
	"""Extract quantity from Sell Order notes like 'Tổng SL: 14 | ...'."""
	import re
	if not notes:
		return None
	m = re.search(r"Tổng SL:\s*(\d+)", notes)
	return int(m.group(1)) if m else None


# ── Endpoint 1: Scanner → ERP ───────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def new_order():
	"""Receive new order from BotPasteDon Scanner webhook.
	Expects JSON body per ERP_API_SPEC.md section 1.
	"""
	api_key = frappe.get_request_header("X-API-Key") or frappe.form_dict.get("api_key")
	bot = _validate_api_key(api_key)

	data = frappe.local.form_dict
	if hasattr(data, "data") and isinstance(data.data, str):
		data = json.loads(data.data)

	# Required fields
	order_id = data.get("orderId")
	platform = data.get("platform")
	if not order_id or not platform:
		frappe.throw("Missing required fields: orderId, platform")

	# Deduplicate
	existing = frappe.db.get_value("Sell Order", {"external_order_id": order_id})
	if existing:
		return {"status": "duplicate", "sell_order": existing}

	# Map channel
	channel_name = _find_channel(platform)

	# Map customer
	customer = _find_or_create_customer(data.get("customerName"))

	# Map game context
	game_context = _find_game_context(data.get("game"), data.get("server"))

	# Map currency item
	currency_item = _find_currency_item(game_context, data.get("itemName"))

	# Parse date
	order_date = frappe.utils.today()
	if data.get("order_date"):
		try:
			order_date = getdate(data["order_date"][:10])
		except Exception:
			pass

	# Pricing
	total_price = float(data.get("total_price", 0) or 0)
	channel_fee = float(data.get("channel_fee") or 0)
	earning = float(data.get("earning") or 0)

	# Calculate Eldorado fees if not provided
	if platform == "Eldorado" and not channel_fee:
		# Eldorado doesn't provide fees — calculate from configured rate
		# Default ~10% withdraw fee for Eldorado
		channel_fee = 0
		withdraw_fee_rate = 0.10  # 10% default
		withdraw_fee = total_price * withdraw_fee_rate
	else:
		withdraw_fee = 0

	# Build sell_items child
	sell_items = []
	if currency_item:
		sell_items.append({
			"currency_item": currency_item,
			"quantity": float(data.get("quantity", 1) or 1),
			"unit_price": float(data.get("unit_price", 0) or 0),
		})
	else:
		# Fallback: create row without currency_item so trader can fix later
		sell_items.append({
			"quantity": float(data.get("quantity", 1) or 1),
			"unit_price": float(data.get("unit_price", 0) or 0),
			"note": data.get("itemName", ""),
		})

	# Determine character field
	character = data.get("character", "")
	customer_btag = character if "#" in character else ""
	customer_ingame = character if character and "#" not in character else ""

	# Force USD for marketplace channels
	sale_currency = "USD"

	# Create Sell Order as BotPasteDon user
	frappe.set_user("BotPasteDon")
	so = frappe.get_doc({
		"doctype": "Sell Order",
		"workflow_state": "Queued",
		"customer": customer,
		"sell_channel": channel_name,
		"sale_currency": sale_currency,
		"order_date": order_date,
		"external_order_id": order_id,
		"order_url": data.get("url"),
		"order_item_title": data.get("itemName"),
		"order_quantity": float(data.get("quantity", 1) or 1),
		"order_unit_price": float(data.get("unit_price", 0) or 0),
		"game_context": game_context,
		"customer_btag_snapshot": customer_btag,
		"customer_ingame_name_snapshot": customer_ingame,
		"channel_fee_native": channel_fee,
		"withdraw_fee_native": withdraw_fee,
		"other_cost_native": 0,
		"earning_native": earning,
		"sell_items": sell_items,
	})
	so.flags.ignore_permissions = True
	so.flags.ignore_mandatory = True
	so.insert()
	frappe.set_user("Guest")

	_log_ws_activity(
		bot_id=bot.name,
		action="new_order",
		status="Info",
		detail=f"Created Sell Order {so.name} for {platform} order {order_id}",
		sell_order=so.name,
		payload=data,
	)

	return {"status": "ok", "sell_order": so.name}


# ── Endpoint 2: ERP → Worker ────────────────────────────────────────────────

@frappe.whitelist()
def trigger_delivery(sell_order, action="normal_delivery", files=None, skip_steps=None):
	"""Trigger delivery worker for a Sell Order.
	Called from ERP UI by authorized users.
	"""
	frappe.only_for(["Trader1", "Trader2", "Game Currency Admin", "System Manager"])

	so = frappe.get_doc("Sell Order", sell_order)
	if so.workflow_state not in ("Claimed", "Payment Confirmed", "In Delivery"):
		frappe.throw(f"Cannot trigger delivery for order in state '{so.workflow_state}'")

	if not so.external_order_id:
		frappe.throw("Sell Order has no external_order_id")

	# Resolve channel → worker URL
	channel_name = frappe.db.get_value("Channel", so.sell_channel, "channel_name") if so.sell_channel else None
	if not channel_name:
		frappe.throw("Sell Order has no channel configured")

	worker_url = _get_worker_url(channel_name)

	# Get delivery qty from order_quantity (webhook)
	delivery_qty = cstr(int(flt(so.order_quantity) or 1))

	# Parse files
	file_list = []
	if files:
		file_list = json.loads(files) if isinstance(files, str) else files

	# Parse skip_steps
	skip_list = []
	if skip_steps:
		skip_list = json.loads(skip_steps) if isinstance(skip_steps, str) else skip_steps

	task_payload = {
		"action": action,
		"order_id": so.external_order_id,
		"order_url": so.order_url or "",
		"delivery_qty": delivery_qty,
		"files": file_list,
		"thread_id": so.name,
		"skip_steps": skip_list,
	}

	# POST to worker
	import requests
	try:
		resp = requests.post(worker_url, json=task_payload, timeout=10)
		resp.raise_for_status()
		worker_resp = resp.json()
	except requests.exceptions.RequestException as e:
		_log_ws_activity(
			bot_id=_get_bot_id_for_channel(channel_name),
			action="trigger_delivery",
			status="Error",
			detail=f"Worker call failed for {so.name}: {e}",
			sell_order=so.name,
			payload=task_payload,
		)
		frappe.throw(f"Worker call failed: {e}")

	# Update workflow state — use .save() to trigger before_save hooks
	# (handle_inventory_lock_unlock("enter") must run to lock inventory)
	so.reload()
	so.workflow_state = "In Delivery"
	so.flags.ignore_permissions = True
	so.save()

	_log_ws_activity(
		bot_id=_get_bot_id_for_channel(channel_name),
		action="trigger_delivery",
		status="Info",
		detail=f"Triggered {action} for {so.name} → worker response: {worker_resp.get('status')}",
		sell_order=so.name,
		payload={"request": task_payload, "response": worker_resp},
	)

	return worker_resp


@frappe.whitelist()
def post_evidence_to_marketplace(sell_order_name, skip_steps=None):
	"""Post Order Evidence files to marketplace (G2G/Eldorado) via worker.
	Gathers evidence files from Order Evidence records and sends to worker.
	"""
	frappe.only_for(["Trader1", "Trader2", "Game Currency Admin", "System Manager"])

	so = frappe.get_doc("Sell Order", sell_order_name)

	if not so.external_order_id:
		frappe.throw("Đơn hàng không có external_order_id")

	# Gather evidence records for worker to download
	evidences = frappe.get_all("Order Evidence",
		filters={"reference_doctype": "Sell Order", "reference_name": sell_order_name},
		fields=["name", "attachment"])
	if not evidences:
		frappe.throw("Chưa có bằng chứng để đăng")

	# Build file list with download URLs
	import os
	erp_url = frappe.get_site_config().get("erp_public_url", "http://192.168.2.228:8000")
	file_list = []
	for e in evidences:
		if not e.attachment:
			continue
		file_list.append({
			"name": os.path.basename(e.attachment),
			"url": f"{erp_url}/api/method/gege_custom.gege_custom.api.botpastedon.get_evidence_file",
			"evidence_id": e.name,
		})
	# Resolve worker URL
	config = frappe.get_site_config()
	channel_name = (frappe.db.get_value("Channel", so.sell_channel, "channel_name") if so.sell_channel else "") or ""
	if "eldo" in channel_name.lower():
		host = config.get("eldo_worker_host", "192.168.2.220")
		port = config.get("eldo_worker_port", 8001)
	else:
		host = config.get("g2g_worker_host", "192.168.2.220")
		port = config.get("g2g_worker_port", 8002)
	worker_url = f"http://{host}:{port}/task"


	# Steps: proof -> qty -> chat
	# Skip qty if already submitted via trigger_delivery
	already_delivered = bool(frappe.get_all('WS Activity Log',
		filters={'reference_sell_order': sell_order_name, 'action': 'trigger_delivery', 'status': 'Info'},
		limit=1))
	skip = ['qty'] if already_delivered else []
	if skip_steps:
		extra = json.loads(skip_steps) if isinstance(skip_steps, str) else skip_steps
		skip += [s for s in extra if s not in skip]

	payload = {
		"action": "post_evidence",
		"order_id": so.external_order_id,
		"order_url": so.order_url or "",
		"delivery_qty": cstr(int(flt(so.order_quantity) or 1)),
		"files": file_list,
		"thread_id": so.name,
		"skip_steps": skip,
		"erp_url": erp_url,
		"erp_api_key": frappe.get_value("Bot Credential",
			{"channel": channel_name, "is_active": 1}, "api_secret") or "",
	}

	# 1. Send to worker FIRST — if fails, order stays in Evidence Uploaded
	import requests
	try:
		resp = requests.post(worker_url, json=payload, timeout=30)
		resp.raise_for_status()
		worker_resp = resp.json()
	except requests.exceptions.RequestException as e:
		_log_ws_activity(
			bot_id=_get_bot_id_for_channel(channel_name),
			action="post_evidence",
			status="Error",
			detail=f"Worker call failed for {so.name}: {e}",
			sell_order=so.name,
			payload=payload,
		)
		frappe.throw(f"Lỗi gửi task đến worker: {e}")

	# 2. Worker accepted — now transition to Delivered
	if so.workflow_state != 'Delivered':
		from frappe.model.workflow import apply_workflow
		so.reload()
		apply_workflow(so, 'Deliver')

	_log_ws_activity(
		bot_id=_get_bot_id_for_channel(channel_name),
		action="post_evidence",
		status="Info",
		detail=f"Sent {len(file_list)} evidence files for {so.name} → worker response: {worker_resp.get('status')}",
		sell_order=so.name,
		payload={"request": payload, "response": worker_resp},
	)

	return {"status": "sent", "files_count": len(file_list)}


# ── Endpoint 2b: File Download ────────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def get_evidence_file(evidence_id):
	"""Serve evidence file for worker to download. Requires API key auth."""
	api_key = frappe.get_request_header("X-API-Key") or frappe.form_dict.get("api_key")
	_validate_api_key(api_key)

	import os
	evidence = frappe.get_doc("Order Evidence", evidence_id)
	if not evidence.attachment:
		frappe.throw("No attachment found")

	file_path = os.path.join(frappe.get_site_path(), evidence.attachment.lstrip("/"))
	if not os.path.exists(file_path):
		frappe.throw("File not found on disk")

	frappe.response["type"] = "download"
	frappe.response["filename"] = os.path.basename(evidence.attachment)
	with open(file_path, "rb") as f:
		frappe.response["filecontent"] = f.read()


# ── Endpoint 3: Worker → ERP (callback) ─────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def delivery_callback():
	"""Receive delivery result from Worker.
	Expects JSON body per ERP_API_SPEC.md section 3.
	"""
	api_key = frappe.get_request_header("X-API-Key") or frappe.form_dict.get("api_key")
	bot = _validate_api_key(api_key)

	data = frappe.local.form_dict
	if hasattr(data, "data") and isinstance(data.data, str):
		data = json.loads(data.data)

	order_id = data.get("order_id")
	thread_id = data.get("thread_id")
	success = data.get("success")
	action = data.get("action", "normal_delivery")

	if not order_id and not thread_id:
		frappe.throw("Missing order_id or thread_id")

	# Find Sell Order by thread_id (our SO name) or external_order_id
	so_name = None
	if thread_id and frappe.db.exists("Sell Order", thread_id):
		so_name = thread_id
	elif order_id:
		so_name = frappe.db.get_value("Sell Order", {"external_order_id": order_id})

	if not so_name:
		_log_ws_activity(
			bot_id=bot.name,
			action="delivery_callback",
			status="Error",
			detail=f"Sell Order not found: thread_id={thread_id}, order_id={order_id}",
			payload=data,
		)
		frappe.throw(f"Sell Order not found for order_id={order_id}")

	# Don't auto-Complete — a separate worker handles that
	# Just log the result; order stays in Delivered state
	if action == "post_evidence":
		new_state = "Delivered"
	else:
		new_state = "Delivered" if success else "Disputed"
		# Use .save() to trigger before_save hooks
		# (_deliver_locked_inventory must run to consume locked inventory)
		so_doc = frappe.get_doc("Sell Order", so_name)
		so_doc.workflow_state = new_state
		so_doc.flags.ignore_permissions = True
		so_doc.save()

	_log_ws_activity(
		bot_id=bot.name,
		action="delivery_callback",
		status="Info" if success else "Warning",
		detail=f"Delivery {'succeeded' if success else 'failed'} for {so_name} ({action})",
		sell_order=so_name,
		payload=data,
	)

	return {"status": "ok", "sell_order": so_name, "new_state": new_state}


# ── Endpoint: status_sync → ERP (mirror marketplace state) ─────────────────

# Workflow states protected from bot auto-override (set by staff, not marketplace).
# These exist because the staff actively chose them; marketplace state changes
# should never roll them back.
_PROTECTED_STATES = {
	"Refunded", "Partially Refunded", "Cancellation Requested",
	"Outstanding", "Payment Pending",
}

# When the SO is currently in one of these states, the bot must NOT auto-update —
# even to a target that would otherwise be safe. Reason: a trader is actively
# moving inventory (locks held by `handle_inventory_lock_unlock("enter")`),
# accounting / lot bookkeeping is mid-flight, and any auto-jump could leak the
# lock or fight the worker callback. Return `manual_required` and log Warning
# so an operator inspects and acts in the ERP UI.
_BLOCK_CURRENT_STATES = {"In Delivery"}

# Whitelist of {current_state: {allowed_target_states}}. Anything outside this
# map (i.e. current = Queued / Claimed / Evidence Uploaded / Cancelled / ...) is
# rejected as `unsafe_transition` — the bot will not jump the staff over those
# stages. PROTECTED + BLOCK checks fire BEFORE this whitelist.
_SAFE_TRANSITIONS = {
	"Delivered":   {"Completed", "Disputed", "Refunded"},
	"Outstanding": {"Completed", "Refunded"},
	"Completed":   {"Disputed"},
	"Disputed":    {"Refunded", "Completed"},
}


def _map_marketplace_to_workflow(platform, mp_state):
	"""Map marketplace state -> ERP workflow_state. Return None to ignore."""
	s = (mp_state or "").lower()
	p = (platform or "").lower()
	if p == "g2g":
		return {
			"completed": "Completed",
			"cancelled": "Refunded",
			"disputed": "Disputed",
		}.get(s)
	if p in ("eldorado", "eldo"):
		return {
			"delivered": "Delivered",
			"completed": "Completed",
			"canceled": "Refunded",
			"disputed": "Disputed",
			# received -> treat as delivered (no transition)
			# pendingdelivery -> ignored (trader handles delivering)
		}.get(s)
	return None


@frappe.whitelist(allow_guest=True)
def status_update():
	"""Receive marketplace state changes from BotPasteDon status_sync.

	Expects JSON:
	  {
	    "platform": "g2g" | "eldorado",
	    "external_order_id": "...",
	    "marketplace_state": "completed" / "cancelled" / "disputed" / ...,
	    "previous_state": "..." (optional),
	    "marketplace_state_at": <iso/epoch> (optional),
	    "raw_payload": {...}
	  }

	Returns one of:
	  - {"status": "updated", "from": .., "to": ..}   — wrote workflow_state
	  - {"status": "no_change", ...}                  — current == target
	  - {"status": "protected", ...}                  — current in _PROTECTED_STATES
	  - {"status": "manual_required", ...}            — current in _BLOCK_CURRENT_STATES
	  - {"status": "unsafe_transition", ...}          — (current, target) outside whitelist
	  - {"status": "ignored", ...}                    — unmapped platform.state
	  - {"status": "no_so", ...}                      — external_order_id not on file

	Audit: WS Activity Log entry for every outcome except `no_change` and
	`no_so` (those would flood the log on every poll cycle).
	"""
	api_key = frappe.get_request_header("X-API-Key") or frappe.form_dict.get("api_key")
	bot = _validate_api_key(api_key)

	data = frappe.local.form_dict
	if hasattr(data, "data") and isinstance(data.data, str):
		data = json.loads(data.data)

	platform = (data.get("platform") or "").lower()
	ext_id = data.get("external_order_id")
	mp_state = (data.get("marketplace_state") or "").lower()

	if not ext_id or not platform or not mp_state:
		frappe.throw("Missing required fields: platform, external_order_id, marketplace_state")

	target = _map_marketplace_to_workflow(platform, mp_state)
	if target is None:
		_log_ws_activity(
			bot_id=bot.name, action="status_update", status="Info",
			detail=f"Ignored unmapped {platform}.{mp_state} (ext_id={ext_id})",
			payload=data,
		)
		return {"status": "ignored", "reason": f"unmapped {platform}/{mp_state}",
		        "external_order_id": ext_id}

	so_name = frappe.db.get_value("Sell Order", {"external_order_id": ext_id})
	if not so_name:
		# Silent — bot polls every marketplace order; many won't have an SO yet.
		return {"status": "no_so", "external_order_id": ext_id}

	current = frappe.db.get_value("Sell Order", so_name, "workflow_state")

	if current in _PROTECTED_STATES:
		_log_ws_activity(
			bot_id=bot.name, action="status_update", status="Info",
			detail=(f"Skipped {so_name}: current state {current} is protected "
			        f"(would_be={target}, mp={platform}.{mp_state})"),
			sell_order=so_name, payload=data,
		)
		return {"status": "protected", "sell_order": so_name,
		        "current": current, "would_be": target}

	if current == target:
		# Silent — fired on every poll for orders that have already converged.
		return {"status": "no_change", "sell_order": so_name, "state": current}

	if current in _BLOCK_CURRENT_STATES:
		# A trader is actively handling delivery / lot accounting. Don't auto-jump.
		_log_ws_activity(
			bot_id=bot.name, action="status_update", status="Warning",
			detail=(f"MANUAL REQUIRED {so_name}: current={current} target={target} "
			        f"(mp={platform}.{mp_state}) — bot will not auto-update from "
			        f"{current}; operator must transition on ERP"),
			sell_order=so_name, payload=data,
		)
		return {"status": "manual_required", "sell_order": so_name,
		        "current": current, "would_be": target,
		        "reason": f"current state '{current}' is staff-owned"}

	if target not in _SAFE_TRANSITIONS.get(current, set()):
		_log_ws_activity(
			bot_id=bot.name, action="status_update", status="Warning",
			detail=(f"UNSAFE {so_name}: current={current} target={target} "
			        f"(mp={platform}.{mp_state}) — transition not in safe whitelist"),
			sell_order=so_name, payload=data,
		)
		return {"status": "unsafe_transition", "sell_order": so_name,
		        "current": current, "would_be": target,
		        "reason": f"transition {current}->{target} not whitelisted"}

	# Whitelisted transition — write workflow_state directly via db.set_value.
	#
	# Why not so_doc.save()? Webhook runs as Guest (allow_guest=True), and
	# Frappe's save() pipeline invokes validate_workflow → get_transitions →
	# check_permission("read") against the pre-save snapshot. The snapshot
	# does NOT carry `flags.ignore_permissions`, so save() raises
	# PermissionError even with frappe.set_user("BotPasteDon") unless that
	# user has Sell Order read role (it does not, by design).
	#
	# This is safe for the whitelisted targets because Sell Order's
	# before_save has NO branch for Delivered/Completed/Disputed/Refunded
	# entries — only `In Delivery → Delivered` fires _deliver_locked_inventory(),
	# and that path is blocked above by _BLOCK_CURRENT_STATES.
	#
	# Trade-off: skipping save() also skips after_save's realtime publish, so
	# the ERP UI won't auto-refresh on this update. Operators see the change
	# on next manual refresh / list reload. Acceptable: status_sync runs once
	# every 30 min, operators are not staring at the same SO continuously.
	frappe.db.set_value("Sell Order", so_name, "workflow_state", target)
	frappe.db.commit()

	_log_ws_activity(
		bot_id=bot.name, action="status_update", status="Info",
		detail=(f"Updated {so_name}: {current} -> {target} "
		        f"(mp={platform}.{mp_state})"),
		sell_order=so_name, payload=data,
	)
	return {"status": "updated", "sell_order": so_name,
	        "from": current, "to": target, "marketplace_state": mp_state}
