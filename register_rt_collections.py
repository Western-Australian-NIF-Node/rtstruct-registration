"""Register the archived RTSTRUCT / SEG scans of one session as OHIF ROI Collections.

Runs inside the XNAT Container Service, launched by an Event Service
subscription on "Scan Created". The Container Service supplies XNAT_HOST,
XNAT_USER and XNAT_PASS (a short-lived alias token for the action user); the
command wrapper supplies the session and project IDs of the scan that fired.

Why it works regardless of upload order: every new scan in the session fires
this once, and each run retries every RTSTRUCT/SEG in the session that is not
yet registered. If the RTSTRUCT arrives before its CT, that run fails on the
missing series and the CT's own event later succeeds. Already-registered
objects are skipped by SOP Instance UID, so re-runs never duplicate.

The archived copy is used deliberately: it carries the site-hashed UIDs that
match the archived CT. The file the user uploaded does not.

Usage (inside the container, or by hand):
    XNAT_HOST=https://xnat XNAT_USER=u XNAT_PASS=p \
        python register_rt_collections.py --project P --session XNAT_E00015 [--dry-run] [-v]
"""
import argparse
import base64
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import pydicom

RT_TYPES = {
    "1.2.840.10008.5.1.4.1.1.481.3": "RTSTRUCT",  # RT Structure Set Storage
    "1.2.840.10008.5.1.4.1.1.66.4": "SEG",        # Segmentation Storage
}
ROI_XSI = "icr:roiCollectionData"
# XNAT files RTSTRUCT/RTPLAN/RTDOSE as rtImageScanData and REG as
# otherDicomScanData; older versions used otScanData. SEG gets segScanData.
NON_IMAGE_SCAN_TYPES = {
    "xnat:rtImageScanData", "xnat:otherDicomScanData", "xnat:otScanData",
    "xnat:segScanData",
}


class Xnat:
    def __init__(self, server, user, password, verbose=False):
        self.server = server.rstrip("/")
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth = f"Basic {token}"
        self.verbose = verbose

    def _request(self, method, path, data=None, content_type=None):
        req = urllib.request.Request(self.server + path, data=data, method=method)
        req.add_header("Authorization", self.auth)
        if content_type:
            req.add_header("Content-Type", content_type)
        if self.verbose:
            print(f"  {method} {path}", file=sys.stderr)
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.status, resp.read()

    def get_json(self, path):
        return json.loads(self._request("GET", path)[1])

    def get_bytes(self, path):
        return self._request("GET", path)[1]

    def put_collection(self, project, session, label, kind, payload):
        q = urllib.parse.urlencode({"type": kind})
        path = f"/xapi/roi/projects/{project}/sessions/{session}/collections/{label}?{q}"
        return self._request("PUT", path, data=payload,
                             content_type="application/octet-stream")


def results(payload):
    return payload.get("ResultSet", {}).get("Result", [])


def existing_collection_uids(xnat, session_id):
    """SOP Instance UIDs of ROI collections already on the session."""
    uids = set()
    for a in results(xnat.get_json(f"/data/experiments/{session_id}/assessors?format=json")):
        if a.get("xsiType") != ROI_XSI:
            continue
        detail = xnat.get_json(f"/data/experiments/{session_id}/assessors/{a['ID']}?format=json")
        for item in detail.get("items", []):
            uid = item.get("data_fields", {}).get("UID")
            if uid:
                uids.add(uid)
    return uids


def candidate_files(xnat, session_id):
    """(scan_id, file_name, uri) for DICOM files in scans XNAT could not type as images."""
    out = []
    for scan in results(xnat.get_json(f"/data/experiments/{session_id}/scans?format=json")):
        if scan.get("xsiType") not in NON_IMAGE_SCAN_TYPES:
            continue
        files = results(xnat.get_json(
            f"/data/experiments/{session_id}/scans/{scan['ID']}/files?format=json"))
        out += [(scan["ID"], f["Name"], f["URI"]) for f in files
                if f.get("Name", "").lower().endswith(".dcm")]
    return out


def safe_label(text):
    return re.sub(r"[^A-Za-z0-9_]", "_", text)


def process_session(xnat, project, session_id, dry_run, verbose):
    try:
        session = xnat.get_json(f"/data/experiments/{session_id}?format=json")
        slabel = session["items"][0]["data_fields"].get("label", session_id)
    except (urllib.error.HTTPError, KeyError, IndexError):
        slabel = session_id
    known = existing_collection_uids(xnat, session_id)
    candidates = candidate_files(xnat, session_id)
    per_scan = {}
    for scan_id, _, _ in candidates:
        per_scan[scan_id] = per_scan.get(scan_id, 0) + 1

    registered = failed = 0
    for scan_id, name, uri in candidates:
        raw = xnat.get_bytes(uri)
        try:
            ds = pydicom.dcmread(io.BytesIO(raw), stop_before_pixels=True)
        except Exception as e:
            print(f"{slabel}/{scan_id}/{name}: unreadable ({e})")
            continue
        kind = RT_TYPES.get(str(ds.SOPClassUID))
        if kind is None:
            continue
        if ds.SOPInstanceUID in known:
            if verbose:
                print(f"{slabel}/{scan_id}: {kind} already registered")
            continue
        label = safe_label(f"{slabel}_{scan_id}_{kind}")
        if per_scan[scan_id] > 1:
            label += "_" + safe_label(ds.SOPInstanceUID[-8:])
        if dry_run:
            print(f"{slabel}/{scan_id}: would register {kind} as {label}")
            registered += 1
            continue
        try:
            status, body = xnat.put_collection(project, session_id, label, kind, raw)
            print(f"{slabel}/{scan_id}: registered {kind} as {label} "
                  f"({status} {body.decode(errors='replace').strip()})")
            known.add(ds.SOPInstanceUID)
            registered += 1
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace").strip()
            # 422 "Missing dependencies" = referenced CT/MR not archived yet.
            # A later Scan Created event for that series will retry.
            print(f"{slabel}/{scan_id}: not registered yet, {kind} as {label} ({e.code} {msg})")
            failed += 1
    return registered, failed


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--project", required=True)
    p.add_argument("--session", required=True, help="session accession ID, e.g. XNAT_E00015")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    host, user, password = (os.environ.get(k) for k in ("XNAT_HOST", "XNAT_USER", "XNAT_PASS"))
    if not (host and user and password):
        sys.exit("XNAT_HOST, XNAT_USER and XNAT_PASS must be set")
    xnat = Xnat(host, user, password, args.verbose)
    try:
        registered, failed = process_session(xnat, args.project, args.session, args.dry_run, args.verbose)
    except urllib.error.HTTPError as e:
        sys.exit(f"{e.code} from {e.url}: session missing or no access for {user}")
    print(f"{'would register' if args.dry_run else 'registered'} {registered}, deferred {failed}")


if __name__ == "__main__":
    main()
