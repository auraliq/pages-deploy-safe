"""Tests with a fake Cloudflare behind the HTTP layer. No network, no real IDs: every value below is made up."""
import io, json, os, re, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import pages_deploy as pd

ACCOUNT, PROJECT, TOKEN = "acct0000", "demo-site", "tok-test-not-real"
ENV = {"CLOUDFLARE_ACCOUNT_ID": ACCOUNT, "CLOUDFLARE_PAGES_PROJECT": PROJECT, "CLOUDFLARE_API_TOKEN": TOKEN}
PROJ = f"/accounts/{ACCOUNT}/pages/projects/{PROJECT}"


class FakeCF:
    """Just enough of the Pages API: project, deployments, upload token, asset store, create deployment."""
    def __init__(self):
        self.deployments = []          # newest first
        self.store = set()
        self.calls = []                # (method, path)
        self.created = []              # parsed multipart fields of every deployment created
        self.fail_next = []            # statuses to return once, in order
        self.on_upsert = None
        self.seq = 0
        self.uploaded = []

    def add_deployment(self, files, env="production", branch="main"):
        self.seq += 1
        d = {"id": f"id-{self.seq:04d}", "short_id": f"s{self.seq:03d}", "environment": env,
             "created_on": f"2026-11-11T10:{self.seq:02d}:00Z", "files": dict(files), "branch": branch}
        self.deployments.insert(0, d)
        return d

    def __call__(self, method, url, headers, data, timeout):
        path = url.replace(pd.API, "")
        self.calls.append((method, path.split("?")[0]))
        if self.fail_next:
            code = self.fail_next.pop(0)
            return code, json.dumps({"success": False, "errors": [{"message": "injected"}]}).encode()
        auth = headers["Authorization"]
        ok = lambda result: (200, json.dumps({"success": True, "result": result}).encode())
        if path.startswith("/pages/assets/"):
            assert auth == "Bearer jwt-abc", "asset calls must use the upload JWT, not the account token"
            body = json.loads(data)
            if path.endswith("check-missing"):
                return ok([h for h in body["hashes"] if h not in self.store])
            if path.endswith("/upload"):
                for item in body:
                    self.store.add(item["key"]); self.uploaded.append(item["key"])
                return ok(None)
            if path.endswith("upsert-hashes"):
                if self.on_upsert: self.on_upsert()
                return ok(None)
        assert auth == f"Bearer {TOKEN}", "project calls must use the API token"
        if method == "GET" and path == PROJ:
            return ok({"production_branch": "main"})
        if method == "GET" and path.startswith(PROJ + "/deployments?"):
            return ok([{k: v for k, v in d.items() if k != "files"} for d in self.deployments])
        m = re.fullmatch(re.escape(PROJ) + r"/deployments/(.+)", path)
        if method == "GET" and m:
            return ok(next(d for d in self.deployments if d["id"] == m.group(1)))
        if method == "GET" and path == PROJ + "/upload-token":
            return ok({"jwt": "jwt-abc"})
        if method == "POST" and path == PROJ + "/deployments":
            boundary = headers["Content-Type"].split("boundary=")[1]
            fields = {}
            for part in data.decode().split(f"--{boundary}")[1:-1]:
                head, _, val = part.strip("\r\n").partition("\r\n\r\n")
                fields[re.search(r'name="([^"]+)"', head).group(1)] = (val, 'filename="' in head)
            self.created.append(fields)
            branch = fields["branch"][0]
            d = self.add_deployment(json.loads(fields["manifest"][0]), "production" if branch == "main" else "preview", branch)
            return ok({k: v for k, v in d.items() if k != "files"} | {"url": f"https://{d['short_id']}.{PROJECT}.pages.dev"})
        raise AssertionError(f"unexpected call {method} {path}")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.site = os.path.join(self.dir, "site")
        os.makedirs(os.path.join(self.site, "docs"))
        self.base_file = os.path.join(self.dir, "base.json")
        self.cf = FakeCF()
        self.lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text, root=None):
        p = os.path.join(root or self.site, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def site_files(self, **extra):
        self.write("index.html", "<h1>home</h1>")
        self.write("docs/a.html", "<p>a</p>")
        self.write("_redirects", "/old /new 301\n")
        self.write("_headers", "/*\n  X-Test: 1\n")

    def hashes(self):
        return {rel: pd.file_hash(p) for rel, p in pd.collect(self.site).items()}

    def run_cli(self, *argv, env=ENV):
        self.lines = []
        code = pd.main(["--base-file", self.base_file, *argv], env=env, http=self.cf, out=self.lines.append, sleep=lambda s: None)
        return code

    @property
    def output(self):
        return "\n".join(self.lines)


class Config(Base):
    def test_missing_env_names_the_variables_and_never_prints_a_token(self):
        code = self.run_cli("status", env={"CLOUDFLARE_API_TOKEN": TOKEN})
        self.assertEqual(code, pd.EXIT_ERROR)
        self.assertIn("CLOUDFLARE_ACCOUNT_ID", self.output)
        self.assertIn("CLOUDFLARE_PAGES_PROJECT", self.output)
        self.assertNotIn(TOKEN, self.output)

    def test_project_flag_overrides_env(self):
        cfg = pd.config_from_env(ENV, project="other")
        self.assertEqual(cfg["project"], "other")

    def test_api_error_is_reported(self):
        self.cf.fail_next = [403]
        code = self.run_cli("snapshot")
        self.assertEqual(code, pd.EXIT_ERROR)
        self.assertIn("403", self.output); self.assertIn("injected", self.output)
        self.assertNotIn(TOKEN, self.output)

    def test_retry_on_503(self):
        self.cf.add_deployment({"/index.html": "h1"})
        self.cf.fail_next = [503]
        self.assertEqual(self.run_cli("snapshot"), pd.EXIT_OK)


class Files(Base):
    def test_collect_skips_specials_and_junk(self):
        self.site_files()
        self.write(".DS_Store", "x"); self.write(pd.BASE_FILE, "{}"); self.write("_worker.js", "export default {}")
        self.assertEqual(sorted(pd.collect(self.site)), ["/docs/a.html", "/index.html"])

    def test_hash_depends_on_content_and_extension(self):
        a = self.write("x.html", "same"); b = self.write("y.txt", "same"); c = self.write("z.html", "same")
        self.assertEqual(len(pd.file_hash(a)), 32)
        self.assertNotEqual(pd.file_hash(a), pd.file_hash(b))
        self.assertEqual(pd.file_hash(a), pd.file_hash(c))

    def test_diff(self):
        d = pd.diff_manifests({"/a": "1", "/b": "2"}, {"/b": "3", "/c": "4"})
        self.assertEqual(d, {"added": ["/c"], "removed": ["/a"], "changed": ["/b"]})


class StaleBase(Base):
    """The incident: a deploy built on an older manifest must not reach production."""
    def setUp(self):
        super().setUp()
        self.site_files()
        self.live = self.cf.add_deployment(self.hashes())

    def test_snapshot_then_deploy_works_and_moves_the_base(self):
        self.assertEqual(self.run_cli("snapshot"), pd.EXIT_OK)
        self.write("docs/b.html", "<p>b</p>")
        self.assertEqual(self.run_cli("deploy", self.site), pd.EXIT_OK, self.output)
        self.assertEqual(len(self.cf.created), 1)
        fields = self.cf.created[0]
        manifest = json.loads(fields["manifest"][0])
        self.assertIn("/docs/b.html", manifest)
        self.assertEqual(fields["branch"][0], "main")
        with open(self.base_file) as f:
            base = json.load(f)
        self.assertEqual(base["deployment_id"], self.cf.deployments[0]["id"])     # base now = the deployment we made

    def test_a_teammate_deploys_after_my_snapshot_so_i_am_refused(self):
        self.run_cli("snapshot")
        teammate = dict(self.hashes()); teammate["/docs/teammate.html"] = "deadbeef" * 4
        self.cf.add_deployment(teammate)                 # teammate publishes after my snapshot
        self.write("docs/mine.html", "<p>mine</p>")
        code = self.run_cli("deploy", self.site)
        self.assertEqual(code, pd.EXIT_REFUSED)
        self.assertIn("REFUSED", self.output); self.assertIn("revert", self.output)
        self.assertEqual(self.cf.created, [], "nothing may be created when the base is stale")
        self.assertFalse([c for c in self.cf.calls if c == ("POST", f"{PROJ}/deployments")])

    def test_no_base_is_refused_when_production_exists(self):
        code = self.run_cli("deploy", self.site)
        self.assertEqual(code, pd.EXIT_REFUSED)
        self.assertIn("snapshot", self.output)
        self.assertEqual(self.cf.created, [])

    def test_first_ever_production_deploy_needs_no_base(self):
        self.cf.deployments.clear()
        self.assertEqual(self.run_cli("deploy", self.site), pd.EXIT_OK, self.output)

    def test_race_during_upload_is_caught(self):
        self.run_cli("snapshot")
        self.write("docs/new.html", "<p>n</p>")
        self.cf.on_upsert = lambda: self.cf.add_deployment({"/x.html": "1" * 32})   # someone deploys while we upload
        code = self.run_cli("deploy", self.site)
        self.assertEqual(code, pd.EXIT_REFUSED)
        self.assertIn("while uploading", self.output)
        self.assertEqual(self.cf.created, [])

    def test_preview_branch_is_not_guarded(self):
        code = self.run_cli("deploy", self.site, "--branch", "try-it")      # no base file at all
        self.assertEqual(code, pd.EXIT_OK, self.output)
        self.assertEqual(self.cf.created[0]["branch"][0], "try-it")
        self.assertEqual(self.cf.deployments[0]["environment"], "preview")
        self.assertFalse(os.path.exists(self.base_file))                    # previews never touch the base

    def test_status_codes(self):
        self.assertEqual(self.run_cli("status"), pd.EXIT_REFUSED)           # no base yet
        self.run_cli("snapshot")
        self.assertEqual(self.run_cli("status"), pd.EXIT_OK)
        self.cf.add_deployment(self.hashes())
        self.assertEqual(self.run_cli("status"), pd.EXIT_REFUSED)
        self.assertIn("STALE", self.output)


class Removals(Base):
    def setUp(self):
        super().setUp()
        self.site_files()
        self.cf.add_deployment({**self.hashes(), "/docs/old.html": "a" * 32})
        self.run_cli("snapshot")

    def test_deleting_live_files_needs_a_flag(self):
        code = self.run_cli("deploy", self.site)
        self.assertEqual(code, pd.EXIT_REFUSED)
        self.assertIn("/docs/old.html", self.output)
        self.assertEqual(self.cf.created, [])

    def test_allow_removals(self):
        self.assertEqual(self.run_cli("deploy", self.site, "--allow-removals"), pd.EXIT_OK, self.output)
        self.assertNotIn("/docs/old.html", json.loads(self.cf.created[0]["manifest"][0]))


class AddOnly(Base):
    def setUp(self):
        super().setUp()
        self.site_files()
        self.teammate = {"/docs/teammate.html": "b" * 32}
        self.cf.add_deployment({**self.hashes(), **self.teammate})
        self.addon = os.path.join(self.dir, "addon")
        self.write("new/page.html", "<p>new</p>", root=self.addon)
        self.write("_redirects", "/a /b 301\n", root=self.addon)

    def test_keeps_everything_in_production_and_adds_the_new_files(self):
        code = self.run_cli("deploy", self.addon, "--add-only")        # no base needed: it starts from the newest production
        self.assertEqual(code, pd.EXIT_OK, self.output)
        manifest = json.loads(self.cf.created[0]["manifest"][0])
        self.assertIn("/docs/teammate.html", manifest)                 # the teammate's file survives
        self.assertIn("/index.html", manifest)
        self.assertIn("/new/page.html", manifest)
        self.assertEqual(len(manifest), 4)

    def test_only_new_files_are_uploaded(self):
        for rel, h in self.hashes().items(): self.cf.store.add(h)
        self.run_cli("deploy", self.addon, "--add-only")
        self.assertEqual(len(self.cf.uploaded), 1)

    def test_overwriting_an_existing_path_is_refused(self):
        self.write("index.html", "<h1>different</h1>", root=self.addon)
        code = self.run_cli("deploy", self.addon, "--add-only")
        self.assertEqual(code, pd.EXIT_REFUSED)
        self.assertIn("--overwrite", self.output)
        self.assertEqual(self.run_cli("deploy", self.addon, "--add-only", "--overwrite"), pd.EXIT_OK, self.output)

    def test_identical_existing_file_is_not_a_clash(self):
        self.write("index.html", "<h1>home</h1>", root=self.addon)
        self.assertEqual(self.run_cli("deploy", self.addon, "--add-only"), pd.EXIT_OK, self.output)

    def test_special_files_from_another_folder(self):
        os.remove(os.path.join(self.addon, "_redirects"))
        self.assertEqual(self.run_cli("deploy", self.addon, "--add-only"), pd.EXIT_REFUSED)
        code = self.run_cli("deploy", self.addon, "--add-only", "--special-from", self.site)
        self.assertEqual(code, pd.EXIT_OK, self.output)
        fields = self.cf.created[0]
        self.assertIn("_redirects", fields); self.assertIn("_headers", fields)
        self.assertTrue(fields["_redirects"][1], "special files go as file parts")

    def test_no_special_files_flag(self):
        os.remove(os.path.join(self.addon, "_redirects"))
        self.assertEqual(self.run_cli("deploy", self.addon, "--add-only", "--no-special-files"), pd.EXIT_OK, self.output)
        self.assertNotIn("_redirects", self.cf.created[0])


class DryRun(Base):
    def test_dry_run_writes_nothing(self):
        self.site_files()
        self.cf.add_deployment(self.hashes())
        self.run_cli("snapshot")
        self.write("docs/c.html", "<p>c</p>")
        code = self.run_cli("deploy", self.site, "--dry-run")
        self.assertEqual(code, pd.EXIT_OK, self.output)
        self.assertIn("DRY RUN", self.output); self.assertIn("nothing uploaded", self.output)
        self.assertEqual(self.cf.created, []); self.assertEqual(self.cf.uploaded, [])
        writes = [c for c in self.cf.calls if c[0] == "POST" and not c[1].endswith("check-missing")]
        self.assertEqual(writes, [])

    def test_dry_run_still_refuses_a_stale_base(self):
        self.site_files()
        self.cf.add_deployment(self.hashes()); self.run_cli("snapshot")
        self.cf.add_deployment(self.hashes())
        self.assertEqual(self.run_cli("deploy", self.site, "--dry-run"), pd.EXIT_REFUSED)

    def test_dry_run_add_only_reports_the_plan(self):
        self.site_files()
        self.cf.add_deployment(self.hashes())
        addon = os.path.join(self.dir, "addon"); self.write("n.html", "x", root=addon)
        self.assertEqual(self.run_cli("deploy", addon, "--add-only", "--dry-run", "--no-special-files"), pd.EXIT_OK, self.output)
        self.assertIn("+1 added", self.output)


class Manifest(Base):
    def test_multipart_body_has_manifest_branch_and_specials(self):
        self.site_files()
        self.assertEqual(self.run_cli("deploy", self.site), pd.EXIT_OK, self.output)
        f = self.cf.created[0]
        self.assertEqual(set(f), {"manifest", "branch", "_redirects", "_headers"})
        self.assertEqual(json.loads(f["manifest"][0]), self.hashes())


if __name__ == "__main__":
    unittest.main()
