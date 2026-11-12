# pages-deploy-safe

A small CLI that deploys a folder to Cloudflare Pages through the Direct Upload API, without wrangler, and **refuses to publish to production unless your manifest is based on the newest production deployment.**

Standard library only. One file: `pages_deploy.py`.

## Why it exists

A Pages deployment is not a patch, it is a full manifest: every path and its hash. I built a deploy from a manifest I had fetched a while earlier. In between, a teammate had published a change. My deploy went out with the old manifest and **silently reverted their work for 3 minutes** until it was noticed. Nothing failed and nothing warned: Pages did exactly what it was asked.

So the rule became code: before it publishes, the tool looks up the newest production deployment, compares it with the one your folder was based on, and stops (exit code 3) if they differ. It checks again after uploading, right before creating the deployment, to catch a deploy that lands in the middle.

## Setup

```bash
export CLOUDFLARE_ACCOUNT_ID=...        # your account id
export CLOUDFLARE_PAGES_PROJECT=...     # the Pages project name (or pass --project)
export CLOUDFLARE_API_TOKEN=...         # token with "Cloudflare Pages: Edit" and nothing else
```

No IDs or tokens live in the code or in the repo. Errors never print the token.

## Use

```bash
python3 pages_deploy.py snapshot                      # record the newest production deployment as your base
# ... change files in ./site ...
python3 pages_deploy.py status                        # is my base still the newest? (exit 0 yes, 3 no)
python3 pages_deploy.py deploy ./site --dry-run       # read everything, print the plan, write nothing
python3 pages_deploy.py deploy ./site                 # publish to production (guarded)
python3 pages_deploy.py deploy ./site --branch try-it # preview deployment (not guarded, never touches production)
```

### Add-only mode

When you only want to add pages and must not touch anything else:

```bash
python3 pages_deploy.py deploy ./new-pages --add-only --special-from ./site
```

The manifest is built as **the newest production manifest plus the files in the folder**. Nothing is removed, so a teammate's files survive, and no base file is needed because it starts from the newest production deployment. It refuses to overwrite a path that already exists with different content unless you pass `--overwrite`.

### What the guard checks

| Situation | Result |
|---|---|
| Your base is the newest production deployment | publishes |
| Someone deployed after your `snapshot` | `REFUSED`, exit 3, nothing created |
| No base recorded and production already exists | `REFUSED`: run `snapshot` first (or use `--add-only`) |
| A deploy lands while you are uploading | `REFUSED` at the second check, nothing created |
| The deploy would delete live files | `REFUSED` unless `--allow-removals` |
| Production deploy without `_redirects` or `_headers` | `REFUSED`: it would drop the live ones. Use `--special-from DIR` or `--no-special-files` |
| Preview branch | no guard (it cannot overwrite production) |

After a successful production deploy the base moves to the deployment you just created, so you can deploy again without a new snapshot. If a teammate deploys, your base goes stale and the tool tells you to merge and snapshot again.

### Exit codes

`0` ok, `1` configuration or API error, `3` refused by the safety check.

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests run against a fake Cloudflare behind the HTTP layer: they reproduce the incident (a teammate publishes after your snapshot, the deploy is refused and no deployment is created), the race during upload, add-only, removals, special files, retries and dry run. They also assert that asset calls use the upload JWT and project calls use the API token.

## Limits

- Hashes are `sha256(content + extension)` truncated to 32 hex characters, not wrangler's blake3. The asset store only needs a stable key per content, but a "changed" file in the plan may just be a different hash scheme.
- It reads the newest production manifest from the deployment's `files` field; if the API does not return it, the tool refuses rather than guessing.
- Deployments are not locked on Cloudflare's side. The two checks make the window very small, they do not make it zero. Use branch protections or a deploy lock if several people deploy at the same moment.
- Functions (`_worker.js`) are sent as a form field but not otherwise inspected. No wrangler features (bindings, local dev).

## License

MIT. Author: Ricardo Delgado (https://www.auraliq.io/ricardo, https://www.linkedin.com/in/ricardo-delgado-0626642a7, ricardo@auraliq.io).
