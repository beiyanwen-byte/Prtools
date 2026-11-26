#!/usr/bin/env python3
"""Line-oriented JSON IPC worker; GitHub credentials remain in this process."""

from __future__ import annotations

import json
import sys

from desktop_backend import DesktopBackend, DesktopBackendError


MAX_REQUEST_CHARS = 100_000


def dispatch(backend, request):
    if not isinstance(request, dict) or not isinstance(request.get("action"), str):
        raise DesktopBackendError("invalid_request", "The desktop request is invalid.")
    action = request["action"]
    if action == "describe_repository":
        return backend.describe_repository(request.get("path", ""))
    if action == "connect_repository":
        return backend.connect_repository(request.get("path", ""), token=request.get("token"),
                                          expected_repository=request.get("expected_repository"))
    if action == "load_branches":
        return {"branches": backend.load_branches()}
    if action == "fetch_remote":
        return backend.fetch_remote()
    if action == "list_branch_tags":
        return backend.list_branch_tags()
    if action == "list_workflow_tag_runs":
        return backend.list_workflow_tag_runs(reset=request.get("reset") is True,
                                              workflow_name=request.get("workflow_name") or "build-and-push")
    if action == "suggest_tag_name":
        return backend.suggest_tag_name(request.get("branch", ""))
    if action == "create_remote_tag":
        return backend.create_remote_tag(
            request.get("branch", ""), request.get("tag_name", ""),
            confirmed=request.get("confirmed") is True)
    if action == "activity":
        return backend.activity()
    if action == "compare":
        return backend.compare(request.get("base"), request.get("source"))
    if action == "source_commit_detail":
        return backend.source_commit_detail(request.get("sha"))
    if action == "search_reviewers":
        return backend.search_reviewers(request.get("query", ""))
    if action == "load_settings":
        return backend.load_settings()
    if action == "save_settings":
        return backend.save_settings(request.get("draft"))
    if action == "remember_branch_selection":
        return backend.remember_branch_selection(request.get("base"), request.get("source"))
    if action == "remember_local_repository_path":
        return backend.remember_local_repository_path(request.get("path", ""))
    if action == "inspect":
        return backend.inspect(request.get("base"), request.get("source"))
    if action == "create":
        return backend.create(
            request.get("base"), request.get("source"),
            request.get("title"), request.get("body"), request.get("reviewers", []),
            confirmed=request.get("confirmed") is True,
        )
    if action == "sign_out":
        backend.sign_out()
        return {"signed_out": True}
    raise DesktopBackendError("not_found", "The requested desktop action is not available.")


def main():
    backend = DesktopBackend()
    for line in sys.stdin:
        if len(line) > MAX_REQUEST_CHARS:
            response = {"ok": False, "error": {
                "code": "request_too_large", "message": "The desktop request is too large."}}
        else:
            try:
                request = json.loads(line)
                data = dispatch(backend, request)
                response = {"ok": True, "data": data}
            except DesktopBackendError as error:
                response = {"ok": False, "error": {"code": error.code, "message": error.message}}
            except (ValueError, TypeError, RecursionError):
                response = {"ok": False, "error": {
                    "code": "invalid_request", "message": "The desktop request is invalid."}}
            except Exception:
                # Never forward a traceback, helper output, or raw exception to
                # the GUI protocol stream.
                response = {"ok": False, "error": {
                    "code": "internal_error", "message": "The desktop backend operation failed."}}
        try:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            backend.sign_out()
            return
    backend.sign_out()


if __name__ == "__main__":
    main()
