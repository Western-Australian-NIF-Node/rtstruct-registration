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
import io
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

import pydicom
import requests
from xnat_api_client import XnatSession
from xnat_api_client.access.core import get_details_json, get_summaries_json
from xnat_api_client.access.scan import list_scan_summaries_in_experiment
from xnat_api_client.interface import ExperimentId
from xnat_api_client.models.scan import ScanId

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


# The client has no assessor or ROI-collection support, so these two go
# through its underlying HTTP session.

def existing_collection_uids(http, session_id):
    """SOP Instance UIDs of ROI collections already on the session."""
    url = f"/data/experiments/{session_id}/assessors"
    return {
        uid
        for a in get_summaries_json(http, url) if a.get("xsiType") == ROI_XSI
        if (uid := get_details_json(http, f"{url}/{a['ID']}").get("UID"))
    }


def put_collection(http, project, session_id, label, kind, payload):
    path = f"/xapi/roi/projects/{project}/sessions/{session_id}/collections/{label}"
    return http.put(path, query={"type": kind}, data=payload,
                    headers={"Content-Type": "application/octet-stream"})


def non_image_scans(http, experiment):
    """Scans XNAT could not type as images, as (scan_id, {file_name: bytes})."""
    with tempfile.TemporaryDirectory() as tmp:
        for summary in list_scan_summaries_in_experiment(http, experiment.id):
            if summary.xsi_type not in NON_IMAGE_SCAN_TYPES:
                continue
            zip_path = Path(tmp, f"{summary.id}.zip")
            experiment.scan(ScanId(summary.id)).download_zip(zip_path)
            with zipfile.ZipFile(zip_path) as z:
                yield summary.id, {Path(n).name: z.read(n) for n in z.namelist()
                                   if n.lower().endswith(".dcm")}


def safe_label(text):
    return re.sub(r"[^A-Za-z0-9_]", "_", text)


def process_session(xnat, project, session_id, dry_run, verbose):
    http = xnat._session
    experiment = xnat.experiment(ExperimentId(session_id))
    slabel = experiment.label
    known = existing_collection_uids(http, session_id)

    registered = failed = 0
    for scan_id, files in non_image_scans(http, experiment):
        for name, raw in files.items():
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
            if len(files) > 1:
                label += "_" + safe_label(ds.SOPInstanceUID[-8:])
            if dry_run:
                print(f"{slabel}/{scan_id}: would register {kind} as {label}")
                registered += 1
                continue
            try:
                resp = put_collection(http, project, session_id, label, kind, raw)
                print(f"{slabel}/{scan_id}: registered {kind} as {label} "
                      f"({resp.status_code} {resp.text.strip()})")
                known.add(ds.SOPInstanceUID)
                registered += 1
            except requests.HTTPError as e:
                # 422 "Missing dependencies" = referenced CT/MR not archived yet.
                # A later Scan Created event for that series will retry.
                print(f"{slabel}/{scan_id}: not registered yet, {kind} as {label} "
                      f"({e.response.status_code} {e.response.text.strip()})")
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
    with XnatSession(host, user, password) as xnat:
        try:
            registered, failed = process_session(xnat, args.project, args.session,
                                                 args.dry_run, args.verbose)
        except requests.HTTPError as e:
            sys.exit(f"{e}: session missing or no access for {user}")
    print(f"{'would register' if args.dry_run else 'registered'} {registered}, deferred {failed}")


if __name__ == "__main__":
    main()
