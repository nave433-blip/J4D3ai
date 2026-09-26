"""Azure management connector (READ-ONLY) via Azure Resource Manager.

Uses a service principal (app registration) with the OAuth2 client-
credentials flow. Setup (Azure portal):

1. App registrations -> New registration (single tenant is fine).
2. Certificates & secrets -> New client secret (copy it once).
3. Subscriptions -> your subscription -> Access control (IAM) ->
   Add role assignment -> **Reader** -> select your app.

Then store the three values (never shipped with Cortana):
  cortana settings set azure_tenant_id <tenant-id>
  cortana settings set azure_client_id <application-id>
  cortana settings set azure_client_secret <secret>   # goes to OS keyring

Deliberately read-only: no start/stop/delete actions are exposed. If you
need power operations, use the Azure portal or CLI — a chat assistant
should not be one confirmation-misclick away from deallocating VMs.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

import keyring
from rich.console import Console

from core.connectors.base import Connector
from core.config import load_config

console = Console()

ARM = "https://management.azure.com"
_KEYRING_SERVICE = "cortana_connector_azure"
_KEYRING_USER = "client_secret"


class AzureConnector(Connector):
    id = "azure"
    display = "Azure (read-only)"
    description = ("List Azure subscriptions, resource groups and VM power "
                   "states via ARM. Service-principal, read-only.")
    auth_kind = "token"
    scopes = []  # ARM audience, not Graph scopes

    # -- credentials ----------------------------------------------------
    def _tenant_id(self) -> str:
        return (load_config().get("azure_tenant_id", "") or "").strip()

    def _client_id(self) -> str:
        return (load_config().get("azure_client_id", "") or "").strip()

    def _client_secret(self) -> str:
        try:
            return keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER) or ""
        except Exception:
            return ""

    def _needs_message(self) -> str:
        missing = [n for n, v in (("azure_tenant_id", self._tenant_id()),
                                  ("azure_client_id", self._client_id()),
                                  ("azure_client_secret", self._client_secret()))
                   if not v]
        if missing:
            return ("Not configured: create a service principal with the Reader "
                    f"role (see docs), then set {', '.join(missing)} "
                    "(client secret is prompted for, never typed into chat).")
        return "Run `/connector connect azure` to validate the service principal."

    def connect(self) -> Dict[str, Any]:
        cfg = load_config()
        tenant = self._tenant_id()
        cid = self._client_id()
        secret = self._client_secret()
        if not tenant or not cid:
            console.print("[yellow]Set azure_tenant_id and azure_client_id first:[/yellow]")
            console.print("  cortana settings set azure_tenant_id <tenant-id>")
            console.print("  cortana settings set azure_client_id <application-id>")
            return {"ok": False, "error": "azure_tenant_id/azure_client_id not set"}
        if not secret:
            secret = console.input("Client secret (stored in OS keyring): ").strip()
            if not secret:
                return {"ok": False, "error": "aborted"}
            try:
                keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, secret)
            except Exception as e:
                return {"ok": False, "error": f"Could not store secret: {e}"}
        token = self._fetch_token(tenant, cid, secret)
        if "error" in token:
            return {"ok": False, "error": token["error"]}
        # Validate by listing subscriptions.
        subs = self._arm_get("/subscriptions", token["access_token"],
                             {"api-version": "2022-12-01"})
        if "error" in subs:
            return {"ok": False, "error": subs["error"]}
        token.update({"tenant_id": tenant, "client_id": cid,
                      "account": f"{len(subs.get('value', []))} subscription(s)"})
        self.save_token(token)
        return {"ok": True, "account": token["account"]}

    def _fetch_token(self, tenant: str, cid: str,
                     secret: str) -> Dict[str, Any]:
        url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        data = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "client_id": cid,
             "client_secret": secret,
             "scope": f"{ARM}/.default"}).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                token = json.loads(r.read().decode())
        except Exception as e:
            return {"error": f"Service-principal login failed: {e}"}
        if "access_token" not in token:
            return {"error": f"Service-principal login failed: {token}"}
        token["obtained_at"] = time.time()
        return token

    def _auth(self) -> str:
        tok = self.get_token() or {}
        if (tok.get("access_token")
                and time.time() < tok.get("obtained_at", 0) + int(tok.get("expires_in", 3600)) - 120):
            return tok["access_token"]
        new = self._fetch_token(self._tenant_id(), self._client_id(),
                                self._client_secret())
        if "error" not in new:
            new.update({"tenant_id": self._tenant_id(),
                        "client_id": self._client_id(),
                        "account": tok.get("account", "")})
            self.save_token(new)
            return new.get("access_token", "")
        return ""

    def _arm_get(self, path: str, token: str,
                 params: Dict[str, str]) -> Dict[str, Any]:
        url = ARM + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode()[:400]
            except Exception:
                detail = str(e)
            return {"error": f"ARM HTTP {e.code}: {detail}"}
        except Exception as e:
            return {"error": str(e)}

    def _get(self, path: str, params: Dict[str, str]) -> Dict[str, Any]:
        tok = self._auth()
        if not tok:
            return {"ok": False, "error": "Not authenticated."}
        res = self._arm_get(path, tok, params)
        if "error" in res:
            return {"ok": False, "error": res["error"]}
        return {"ok": True, "data": res}

    # -- actions ----------------------------------------------------------
    def actions(self) -> Dict[str, Dict[str, Any]]:
        def _subscriptions():
            r = self._get("/subscriptions", {"api-version": "2022-12-01"})
            if not r["ok"]:
                return r
            return {"ok": True, "subscriptions": [
                {"id": s.get("subscriptionId"), "name": s.get("displayName"),
                 "state": s.get("state")}
                for s in r["data"].get("value", [])]}

        def _resource_groups(subscription_id: str):
            r = self._get(f"/subscriptions/{subscription_id}/resourcegroups",
                          {"api-version": "2021-04-01"})
            if not r["ok"]:
                return r
            return {"ok": True, "resource_groups": [
                {"name": g.get("name"), "location": g.get("location")}
                for g in r["data"].get("value", [])]}

        def _vms(subscription_id: str, resource_group: str = ""):
            base = (f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
                    if resource_group else f"/subscriptions/{subscription_id}")
            r = self._get(base + "/providers/Microsoft.Compute/virtualMachines",
                          {"api-version": "2023-07-01"})
            if not r["ok"]:
                return r
            out = []
            for vm in r["data"].get("value", []):
                # Power state needs instanceView; fetch per VM (bounded list).
                iv = self._get(vm["id"] + "/instanceView",
                               {"api-version": "2023-07-01"})
                power = ""
                if iv.get("ok"):
                    for s in iv["data"].get("statuses", []):
                        code = s.get("code", "")
                        if code.startswith("PowerState/"):
                            power = code.split("/", 1)[1]
                out.append({"name": vm.get("name"),
                            "size": (vm.get("properties") or {}).get("hardwareProfile", {}).get("vmSize", ""),
                            "power_state": power or "unknown"})
            return {"ok": True, "vms": out}

        return {
            "list_subscriptions": {"description": "List Azure subscriptions.",
                                   "run": _subscriptions},
            "list_resource_groups": {"description": "List resource groups (subscription_id).",
                                     "run": _resource_groups},
            "list_vms": {"description": "List VMs + power states (subscription_id, [resource_group]).",
                         "run": _vms},
        }

    def clear_token(self) -> None:
        super().clear_token()
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USER)
        except Exception:
            pass
