# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# MIT License. See license.txt

from __future__ import unicode_literals
"""
Boot session from cache or build

Session bootstraps info needed by common client side activities including
permission, homepage, default variables, system defaults etc
"""
import frappe, json
from frappe import _
import frappe.utils
from frappe.utils import cint, cstr
import frappe.model.meta
import frappe.defaults
import frappe.translate
import frappe.change_log
import redis
from urllib import unquote

@frappe.whitelist()
def clear(user=None):
	frappe.local.session_obj.update(force=True)
	frappe.local.db.commit()
	clear_cache(frappe.session.user)
	clear_global_cache()
	frappe.response['message'] = _("Cache Cleared")

def clear_cache(user=None):
	cache = frappe.cache()

	if user:
		cache.delete_keys("user:" + user)
		cache.delete_keys("user_doc:" + user)
		frappe.defaults.clear_cache(user)
	else:
		cache.delete_keys("user:")
		cache.delete_keys("user_doc:")
		clear_global_cache()
		frappe.defaults.clear_cache()

def clear_global_cache():
	frappe.model.meta.clear_cache()
	frappe.cache().delete_value(["app_hooks", "installed_apps", "app_modules", "module_app", "time_zone"])
	frappe.setup_module_map()

def clear_sessions(user=None, keep_current=False):
	if not user:
		user = frappe.session.user

	for sid in frappe.db.sql("""select sid from tabSessions where user=%s""", (user,)):
		if keep_current and frappe.session.sid==sid[0]:
			continue
		else:
			delete_session(sid[0])

def delete_session(sid=None, user=None):
	if not user:
		user = hasattr(frappe.local, "session") and frappe.session.user or "Guest"
	frappe.cache().delete_value("session:" + sid, user=user)
	frappe.cache().delete_value("last_db_session_update:" + sid)
	frappe.db.sql("""delete from tabSessions where sid=%s""", sid)

def clear_all_sessions():
	"""This effectively logs out all users"""
	frappe.only_for("Administrator")
	for sid in frappe.db.sql_list("select sid from `tabSessions`"):
		delete_session(sid)

def clear_expired_sessions():
	"""This function is meant to be called from scheduler"""
	for sid in frappe.db.sql_list("""select sid
		from tabSessions where TIMEDIFF(NOW(), lastupdate) > TIME(%s)""", get_expiry_period()):
		delete_session(sid)

def get():
	"""get session boot info"""
	from frappe.desk.notifications import \
		get_notification_info_for_boot, get_notifications
	from frappe.boot import get_bootinfo

	bootinfo = None
	if not getattr(frappe.conf,'disable_session_cache', None):
		# check if cache exists
		bootinfo = frappe.cache().get_value("bootinfo", user=True)
		if bootinfo:
			bootinfo['from_cache'] = 1
			bootinfo["notification_info"].update(get_notifications())
			bootinfo["user"]["recent"] = json.dumps(frappe.cache().get_value("recent:" + frappe.session.user))

	if not bootinfo:
		# if not create it
		bootinfo = get_bootinfo()
		bootinfo["notification_info"] = get_notification_info_for_boot()
		frappe.cache().set_value("bootinfo", bootinfo, user=True)
		try:
			frappe.cache().ping()
		except redis.exceptions.ConnectionError:
			message = _("Redis cache server not running. Please contact Administrator / Tech support")
			if 'messages' in bootinfo:
				bootinfo['messages'].append(message)
			else:
				bootinfo['messages'] = [message]

		# check only when clear cache is done, and don't cache this
		if frappe.local.request:
			bootinfo["change_log"] = frappe.change_log.get_change_log()

	bootinfo["metadata_version"] = frappe.cache().get_value("metadata_version")
	if not bootinfo["metadata_version"]:
		bootinfo["metadata_version"] = frappe.reset_metadata_version()

	for hook in frappe.get_hooks("extend_bootinfo"):
		frappe.get_attr(hook)(bootinfo=bootinfo)

	bootinfo["lang"] = frappe.translate.get_user_lang()

	return bootinfo

class Session:
	def __init__(self, user, resume=False, full_name=None):
		self.sid = cstr(frappe.form_dict.get('sid') or unquote(frappe.request.cookies.get('sid', 'Guest')))
		self.user = user
		self.full_name = full_name
		self.data = frappe._dict({'data': frappe._dict({})})
		self.time_diff = None

		# set local session
		frappe.local.session = self.data

		if resume:
			self.resume()
		else:
			self.start()

	def start(self):
		"""start a new session"""
		# generate sid
		if self.user=='Guest':
			sid = 'Guest'
		else:
			sid = frappe.generate_hash()

		self.data.user = self.user
		self.data.sid = sid
		self.data.data.user = self.user
		self.data.data.session_ip = frappe.local.request_ip
		if self.user != "Guest":
			self.data.data.last_updated = frappe.utils.now()
			self.data.data.session_expiry = get_expiry_period()
			self.data.data.full_name = self.full_name
		self.data.data.session_country = get_geo_ip_country(frappe.local.request_ip)

		# insert session
		if self.user!="Guest":
			self.insert_session_record()

			# update user
			frappe.db.sql("""UPDATE tabUser SET last_login = %s, last_ip = %s
				where name=%s""", (frappe.utils.now(), frappe.local.request_ip, self.data['user']))
			frappe.db.commit()

	def insert_session_record(self):
		frappe.db.sql("""insert into tabSessions
			(sessiondata, user, lastupdate, sid, status)
			values (%s , %s, NOW(), %s, 'Active')""",
				(str(self.data['data']), self.data['user'], self.data['sid']))

		# also add to memcache
		frappe.cache().set_value("session:" + self.data.sid, self.data, user=self.user)

	def resume(self):
		"""non-login request: load a session"""
		import frappe

		data = self.get_session_record()
		if data:
			# set language
			self.data.update({'data': data, 'user':data.user, 'sid': self.sid})
		else:
			self.start_as_guest()

		if self.sid != "Guest":
			frappe.local.lang = frappe.translate.get_user_lang(self.data.user)

	def get_session_record(self):
		"""get session record, or return the standard Guest Record"""
		from frappe.auth import clear_cookies
		r = self.get_session_data()
		if not r:
			frappe.response["session_expired"] = 1
			clear_cookies()
			self.sid = "Guest"
			r = self.get_session_data()

		return r

	def get_session_data(self):
		if self.sid=="Guest":
			return frappe._dict({"user":"Guest"})

		data = self.get_session_data_from_cache()
		if not data:
			data = self.get_session_data_from_db()
		return data

	def get_session_data_from_cache(self):
		data = frappe._dict(frappe.cache().get_value("session:" + self.sid, user=self.user) or {})
		if data:
			session_data = data.get("data", {})
			self.time_diff = frappe.utils.time_diff_in_seconds(frappe.utils.now(),
				session_data.get("last_updated"))
			expiry = self.get_expiry_in_seconds(session_data.get("session_expiry"))

			if self.time_diff > expiry:
				self.delete_session()
				data = None

		return data and data.data

	def get_session_data_from_db(self):
		rec = frappe.db.sql("""select user, sessiondata
			from tabSessions where sid=%s and
			TIMEDIFF(NOW(), lastupdate) < TIME(%s)""", (self.sid,
				get_expiry_period()))
		if rec:
			data = frappe._dict(eval(rec and rec[0][1] or '{}'))
			data.user = rec[0][0]
		else:
			self.delete_session()
			data = None

		return data

	def get_expiry_in_seconds(self, expiry):
		if not expiry: return 3600
		parts = expiry.split(":")
		return (cint(parts[0]) * 3600) + (cint(parts[1]) * 60) + cint(parts[2])

	def delete_session(self):
		delete_session(self.sid, user=self.user)

	def start_as_guest(self):
		"""all guests share the same 'Guest' session"""
		self.user = "Guest"
		self.start()

	def update(self, force=False):
		"""extend session expiry"""
		if (frappe.session['user'] == "Guest" or frappe.form_dict.cmd=="logout"):
			return

		now = frappe.utils.now()

		self.data['data']['last_updated'] = now
		self.data['data']['lang'] = unicode(frappe.lang)

		# update session in db
		last_updated = frappe.cache().get_value("last_db_session_update:" + self.sid)
		time_diff = frappe.utils.time_diff_in_seconds(now, last_updated) if last_updated else None

		# database persistence is secondary, don't update it too often
		updated_in_db = False
		if force or (time_diff==None) or (time_diff > 600):
			frappe.db.sql("""update tabSessions set sessiondata=%s,
				lastupdate=NOW() where sid=%s""" , (str(self.data['data']),
				self.data['sid']))

			frappe.cache().set_value("last_db_session_update:" + self.sid, now)
			updated_in_db = True

		# set in memcache
		frappe.cache().set_value("session:" + self.sid, self.data, user=self.user)

		return updated_in_db

def get_expiry_period():
	exp_sec = frappe.defaults.get_global_default("session_expiry") or "06:00:00"

	# incase seconds is missing
	if len(exp_sec.split(':')) == 2:
		exp_sec = exp_sec + ':00'

	return exp_sec

def get_geo_from_ip(ip_addr):
	try:
		from geoip import geolite2
		return geolite2.lookup(ip_addr)
	except ImportError:
		return
	except ValueError:
		return

def get_geo_ip_country(ip_addr):
	match = get_geo_from_ip(ip_addr)
	if match:
		return match.country
