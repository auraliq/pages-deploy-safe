#!/usr/bin/env python3
"""pages-deploy-safe: deploy a folder to Cloudflare Pages by Direct Upload, without wrangler, and refuse to
publish to production on top of a stale base.

Why: a deploy built from an older manifest silently reverted a teammate's changes for 3 minutes. A Pages
deployment is a full manifest (path -> hash). If yours was made before someone else's deploy, publishing it
throws their files away. This tool checks, right before it publishes, that your manifest is based on the NEWEST
production deployment, and stops with exit code 3 if it is not.

Environment (nothing is hard-coded):
    CLOUDFLARE_ACCOUNT_ID       your account id
    CLOUDFLARE_PAGES_PROJECT    the Pages project name (or --project)
    CLOUDFLARE_API_TOKEN        an API token with "Cloudflare Pages: Edit" and nothing else

Commands:
    snapshot                 save the newest production deployment as your base (.pages-deploy-base.json)
    status                   is your base still the newest production deployment?
    deploy DIR               upload DIR and create a deployment
        --add-only           manifest = newest production manifest + the files in DIR (nothing is removed)
        --dry-run            read everything, print the plan, write nothing
        --branch NAME        branch to deploy (default: the project's production branch)

Standard library only.
"""
import argparse, base64, hashlib, json, mimetypes, os, sys, time, urllib.error, urllib.request, uuid

API = "https://api.cloudflare.com/client/v4"
ASSETS = API + "/pages/assets/"
SPECIAL = ("_redirects", "_headers", "_routes.json", "_worker.js")     # sent as form fields, not as manifest entries
BASE_FILE = ".pages-deploy-base.json"
IGNORE_NAMES = {".DS_Store", BASE_FILE}
MAX_BATCH_BYTES = 20_000_000
EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 1, 3


class ConfigError(Exception): pass
class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
class Refused(Exception):
    """The safety check said no. Nothing was published."""


# ---------------- config ----------------
def config_from_env(env, project=None):
    cfg = {"account": env.get("CLOUDFLARE_ACCOUNT_ID", "").strip(),
           "project": (project or env.get("CLOUDFLARE_PAGES_PROJECT", "")).strip(),
           "token": env.get("CLOUDFLARE_API_TOKEN", "").strip()}
    missing = [n for n, k in (("CLOUDFLARE_ACCOUNT_ID", "account"), ("CLOUDFLARE_PAGES_PROJECT or --project", "project"),
                              ("CLOUDFLARE_API_TOKEN", "token")) if not cfg[k]]
    if missing:
        raise ConfigError("missing: " + ", ".join(missing))
    return cfg


# ---------------- HTTP layer (replaceable in tests) ----------------
def urllib_http(method, url, headers, data, timeout):
    """-> (status, body bytes). Never raises on HTTP errors."""
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Cloudflare:
    def __init__(self, cfg, http=urllib_http, sleep=time.sleep):
        self.cfg, self.http, self.sleep = cfg, http, sleep
        self.project_url = f"{API}/accounts/{cfg['account']}/pages/projects/{cfg['project']}"

    def call(self, method, url, token=None, body=None, ctype="application/json", timeout=300):
        data = body
        if ctype == "application/json" and body is not None:
            data = json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {token or self.cfg['token']}", "Content-Type": ctype}
        for attempt in range(3):
            status, raw = self.http(method, url, headers, data, timeout)
            if status in (429, 500, 502, 503, 504) and attempt < 2:
                self.sleep(2 * (attempt + 1)); continue
            break
        try:
            doc = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except ValueError:
            doc = {}
        if status >= 300 or doc.get("success") is False:
            errs = "; ".join(str(e.get("message", e)) for e in doc.get("errors", [])) or raw[:200].decode("utf-8", "replace")
            raise ApiError(status, errs)
        return doc.get("result")

    def project(self):
        return self.call("GET", self.project_url)

    def newest_production(self):
        rows = self.call("GET", self.project_url + "/deployments?env=production&per_page=10") or []
        prod = [d for d in rows if d.get("environment") == "production"]
        prod.sort(key=lambda d: d.get("created_on", ""), reverse=True)
        return prod[0] if prod else None

    def deployment(self, dep_id):
        return self.call("GET", f"{self.project_url}/deployments/{dep_id}")

    def upload_jwt(self):
        return self.call("GET", self.project_url + "/upload-token")["jwt"]

    def check_missing(self, jwt, hashes):
        return self.call("POST", ASSETS + "check-missing", jwt, {"hashes": hashes}) or []

    def upload(self, jwt, items):
        self.call("POST", ASSETS + "upload", jwt, items)

    def upsert(self, jwt, hashes):
        self.call("POST", ASSETS + "upsert-hashes", jwt, {"hashes": hashes})

    def create_deployment(self, fields):
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in fields.items():
            disp = f'form-data; name="{k}"' + (f'; filename="{k}"' if k in SPECIAL else "")
            parts.append(f"--{boundary}\r\nContent-Disposition: {disp}\r\n\r\n{v}\r\n")
        body = ("".join(parts) + f"--{boundary}--\r\n").encode()
        return self.call("POST", self.project_url + "/deployments", body=body, ctype=f"multipart/form-data; boundary={boundary}")


# ---------------- files and manifests ----------------
def file_hash(path):
    """32 hex chars from sha256(content + extension). The asset store only needs a stable key per content."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read() + os.path.splitext(path)[1].encode()).hexdigest()[:32]


def collect(folder):
    """-> {'/rel/path': absolute path}. Special files and junk are left out."""
    out = {}
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if d != ".git"]
        for n in names:
            if n in IGNORE_NAMES: continue
            p = os.path.join(root, n)
            rel = os.path.relpath(p, folder).replace(os.sep, "/")
            if rel in SPECIAL: continue
            out["/" + rel] = p
    return out


def read_specials(folder):
    out = {}
    for n in SPECIAL:
        p = os.path.join(folder, n)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                out[n] = f.read()
    return out


def diff_manifests(old, new):
    return {"added": sorted(set(new) - set(old)), "removed": sorted(set(old) - set(new)),
            "changed": sorted(k for k in set(old) & set(new) if old[k] != new[k])}


def load_base(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_base(path, dep, files):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"deployment_id": dep["id"], "short_id": dep.get("short_id"), "created_on": dep.get("created_on"), "files": files}, f, indent=0)


def short(dep):
    return f"{dep.get('short_id') or dep['id'][:8]} ({dep.get('created_on', '?')})"


def preview(paths, limit=8):
    shown = ", ".join(paths[:limit])
    return shown + (f" ... +{len(paths) - limit} more" if len(paths) > limit else "")


# ---------------- the safety check ----------------
def check_base(base, newest):
    """Production deploys must sit on top of the newest production deployment. Raises Refused otherwise."""
    if newest is None:
        return
    if base is None:
        raise Refused("no base recorded. Run `snapshot` first (it stores the newest production deployment as your base), "
                      "apply your changes on top of it, then deploy. Or use --add-only.")
    if base["deployment_id"] != newest["id"]:
        raise Refused(f"your base is {base.get('short_id')} ({base.get('created_on')}) but the newest production deployment is "
                      f"{short(newest)}. Someone published after you took your base; deploying now would silently revert their changes. "
                      "Merge their files into your folder, run `snapshot` again, then deploy.")


# ---------------- commands ----------------
def cmd_snapshot(cf, args, out):
    newest = cf.newest_production()
    if newest is None:
        out("No production deployment exists yet: nothing to take as a base."); return EXIT_OK
    files = cf.deployment(newest["id"]).get("files")
    if not files:
        raise Refused("the API did not return the manifest (files) of the newest production deployment")
    save_base(args.base_file, newest, files)
    out(f"base saved: {short(newest)}, {len(files)} files -> {args.base_file}")
    return EXIT_OK


def cmd_status(cf, args, out):
    newest = cf.newest_production()
    base = load_base(args.base_file)
    if newest is None:
        out("no production deployment yet"); return EXIT_OK
    out(f"newest production: {short(newest)}")
    if base is None:
        out("base: none (run `snapshot`)"); return EXIT_REFUSED
    if base["deployment_id"] == newest["id"]:
        out("base is current: safe to deploy"); return EXIT_OK
    out(f"base is STALE ({base.get('short_id')}, {base.get('created_on')}): someone published after you")
    return EXIT_REFUSED


def build_plan(cf, args):
    folder = os.path.abspath(args.dir)
    if not os.path.isdir(folder):
        raise ConfigError(f"not a folder: {args.dir}")
    project = cf.project()
    prod_branch = project.get("production_branch") or "main"
    branch = args.branch or prod_branch
    is_prod = branch == prod_branch
    local = collect(folder)
    if not local:
        raise ConfigError("the folder has no files to deploy")
    local_hashes = {rel: file_hash(p) for rel, p in local.items()}
    newest = cf.newest_production()
    newest_files = {}
    if newest is not None and (is_prod or args.add_only):
        newest_files = cf.deployment(newest["id"]).get("files") or {}
        if not newest_files:
            raise Refused("the API did not return the manifest (files) of the newest production deployment")
    plan = {"folder": folder, "branch": branch, "is_prod": is_prod, "newest": newest, "local": local, "local_hashes": local_hashes}
    if args.add_only:
        clash = [k for k, h in local_hashes.items() if k in newest_files and newest_files[k] != h]
        if clash and not args.overwrite:
            raise Refused(f"--add-only would overwrite {len(clash)} file(s) that already exist in production with other content: "
                          f"{preview(clash)}. Pass --overwrite if that is intended.")
        manifest = dict(newest_files); manifest.update(local_hashes)
        plan["diff"] = diff_manifests(newest_files, manifest)
    else:
        manifest = dict(local_hashes)
        if is_prod:
            check_base(load_base(args.base_file), newest)
            plan["diff"] = diff_manifests(newest_files, manifest)
            if plan["diff"]["removed"] and not args.allow_removals:
                raise Refused(f"this deploy would remove {len(plan['diff']['removed'])} live file(s): {preview(plan['diff']['removed'])}. "
                              "Pass --allow-removals if you mean to delete them.")
        else:
            plan["diff"] = None
    special_dir = os.path.abspath(args.special_from) if args.special_from else folder
    specials = {} if args.no_special_files else read_specials(special_dir)
    if is_prod and not args.no_special_files and not ({"_redirects", "_headers"} & set(specials)):
        raise Refused("no _redirects or _headers found: a production deployment without them would drop the live ones. "
                      "Put them in the folder, pass --special-from DIR, or pass --no-special-files if the site has none.")
    plan["manifest"], plan["specials"], plan["newest_files"] = manifest, specials, newest_files
    return plan


def print_plan(plan, out, dry):
    out(("DRY RUN: " if dry else "") + f"branch {plan['branch']} ({'PRODUCTION' if plan['is_prod'] else 'preview'}), "
        f"{len(plan['manifest'])} files in the manifest, {len(plan['local'])} from the folder")
    if plan["newest"]:
        out(f"newest production: {short(plan['newest'])}")
    d = plan.get("diff")
    if d:
        out(f"vs production: +{len(d['added'])} added, ~{len(d['changed'])} changed, -{len(d['removed'])} removed")
        for k in ("added", "changed", "removed"):
            if d[k]: out(f"  {k}: {preview(d[k])}")
        if d["changed"]:
            out("  (a 'changed' file may only be a different hash scheme: the store keys are not wrangler's)")


def cmd_deploy(cf, args, out):
    plan = build_plan(cf, args)
    print_plan(plan, out, args.dry_run)
    jwt = cf.upload_jwt()
    by_hash = {h: plan["local"][rel] for rel, h in plan["local_hashes"].items()}
    missing = cf.check_missing(jwt, list(by_hash)) if by_hash else []
    out(f"{len(missing)} of {len(by_hash)} files need uploading ({sum(os.path.getsize(by_hash[h]) for h in missing)} bytes)")
    if args.dry_run:
        out("dry run: nothing uploaded, nothing deployed"); return EXIT_OK
    batch, size = [], 0
    for i, h in enumerate(missing):
        with open(by_hash[h], "rb") as f:
            data = f.read()
        batch.append({"key": h, "value": base64.b64encode(data).decode(), "base64": True,
                      "metadata": {"contentType": mimetypes.guess_type(by_hash[h])[0] or "application/octet-stream"}})
        size += len(data)
        if size > MAX_BATCH_BYTES or i == len(missing) - 1:
            cf.upload(jwt, batch); batch, size = [], 0
    cf.upsert(jwt, sorted(set(plan["manifest"].values())))
    if plan["is_prod"]:      # second look, right before publishing: someone may have deployed while we uploaded
        again = cf.newest_production()
        if (again or {}).get("id") != (plan["newest"] or {}).get("id"):
            raise Refused(f"a production deployment appeared while uploading ({short(again)}). Nothing was published. Take a new base and retry.")
    fields = {"manifest": json.dumps(plan["manifest"]), "branch": plan["branch"]}
    fields.update(plan["specials"])
    res = cf.create_deployment(fields)
    out(f"deployed {res.get('short_id', res.get('id'))} {res.get('environment')} {res.get('url')}")
    if plan["is_prod"]:
        save_base(args.base_file, res, plan["manifest"])
        out(f"base moved to the new deployment ({args.base_file})")
    return EXIT_OK


# ---------------- CLI ----------------
def parser():
    ap = argparse.ArgumentParser(prog="pages_deploy.py", description="Safe Direct Upload deploys to Cloudflare Pages")
    ap.add_argument("--project", help="Pages project name (default: $CLOUDFLARE_PAGES_PROJECT)")
    ap.add_argument("--base-file", default=BASE_FILE, help=f"where the base is stored (default {BASE_FILE})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot", help="record the newest production deployment as your base")
    sub.add_parser("status", help="is your base still the newest production deployment?")
    d = sub.add_parser("deploy", help="upload a folder and create a deployment")
    d.add_argument("dir")
    d.add_argument("--branch", help="default: the project's production branch")
    d.add_argument("--add-only", action="store_true", help="newest production manifest + the files in DIR, nothing removed")
    d.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    d.add_argument("--overwrite", action="store_true", help="with --add-only: allow replacing files that exist with other content")
    d.add_argument("--allow-removals", action="store_true", help="allow a production deploy that deletes live files")
    d.add_argument("--special-from", help="folder holding _redirects, _headers, _routes.json, _worker.js (default: DIR)")
    d.add_argument("--no-special-files", action="store_true", help="the site has no _redirects/_headers")
    return ap


def main(argv=None, env=None, http=urllib_http, out=print, sleep=time.sleep):
    args = parser().parse_args(argv)
    try:
        cf = Cloudflare(config_from_env(os.environ if env is None else env, args.project), http=http, sleep=sleep)
        return {"snapshot": cmd_snapshot, "status": cmd_status, "deploy": cmd_deploy}[args.cmd](cf, args, out)
    except Refused as e:
        out(f"REFUSED: {e}"); return EXIT_REFUSED
    except (ConfigError, ApiError) as e:
        out(f"error: {e}"); return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
