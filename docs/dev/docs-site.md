# Docs site: GitHub Pages build and setup runbook

The Primer user docs are a standalone static site. They are no longer served
from inside the app; the operator console links out to the published site.
This runbook covers what the site is, how it is built, and the one-time setup a
human must do to wire up deployment.

## What it is

- Source content lives in `primer/user_docs/` (markdown).
- The build tooling lives in `scripts/docs/`:
  - `scripts/docs/build_site.py` renders the markdown into a static site
    (per-doc `index.html` pages, `404.html`, `sitemap.xml`,
    `search-index.json`, and `assets/docs.css` + `assets/docs.js`). It also
    handles the docs directives (callouts, tabs, mermaid diagrams, ai-doc
    blocks).
  - `scripts/docs/capture_embeds.py` renders the live UI component embeds into
    light/dark PNGs under `_embeds/` (it spins up its own throwaway HTTP server
    and a headless Chromium via Playwright).
- The operator console no longer ships an in-app docs viewer. The console's
  "Docs" link opens the published site in a new tab. That URL comes from the
  `docs_url` setting in `primer/api/config.py`, overridable with the
  `PRIMER_DOCS_URL` environment variable. The default is
  `https://primerhq.github.io/` (the `primerhq` org).

## One-time release setup

Do these once, in order. Steps 1-4 are GitHub configuration; step 5 is a code
or env change so the console links to the real site.

1. **Pick an available GitHub org name.** The site is published to a GitHub
   user/org Pages site, so the org name becomes part of the public URL
   (`https://<DOCS_ORG>.github.io/`). Note that `primer` is already taken (it is
   GitHub's own design system org). DONE: the org `primerhq` was created for
   this (`https://primerhq.github.io/`).

2. **Create the org and the Pages repo.** Create the chosen org, then create a
   repo in it named exactly `<DOCS_ORG>.github.io`. In that repo's
   Settings -> Pages, enable GitHub Pages with the source set to the
   `main` branch, root folder (both the manual first deploy and the deploy
   workflow publish to `main`). DONE: `primerhq/primerhq.github.io` was created
   and seeded with an initial manual build.

3. **Create a deploy token and add it as a secret.** Create a Personal Access
   Token (classic or fine-grained) with write access to the
   `<DOCS_ORG>/<DOCS_ORG>.github.io` repo. In the `codemug/primer` repo, add it
   under Settings -> Secrets and variables -> Actions as the secret
   `DOCS_DEPLOY_TOKEN`.

4. **Set the org repo variable.** In the `codemug/primer` repo, under
   Settings -> Secrets and variables -> Actions -> Variables, add a variable
   named `DOCS_ORG` set to the org name from step 1. The workflow uses it to
   target `${{ vars.DOCS_ORG }}/${{ vars.DOCS_ORG }}.github.io`.

5. **Point the console at the real site.** DONE: the `docs_url` default in
   `primer/api/config.py` is set to `https://primerhq.github.io/`. Override
   with `PRIMER_DOCS_URL` in the deployment environment if the site moves.

## How deploys trigger

`.github/workflows/docs.yml` (maintained on the release-engineering branch
until release) runs on push to `main` whenever it touches docs paths
(`primer/user_docs/**`, `scripts/docs/**`, or the workflow file itself). It
installs the `docs` dependency group, installs Playwright Chromium, runs
`build_site.py` then `capture_embeds.py` into `dist/docs`, and deploys that
directory to the `main` branch of the external Pages repo via
`peaceiris/actions-gh-pages`.

## Build and preview locally

```sh
uv sync --group docs
uv run python -m scripts.docs.build_site primer/user_docs dist/docs
uv run python -m scripts.docs.capture_embeds dist/docs
```

Then serve the output and open it in a browser:

```sh
uv run python -m http.server -d dist/docs 8001
```

## Known merge-time follow-up

DONE: the unit-test sweep in `release.yml` (on the release-engineering branch)
now runs `uv sync --dev --group docs` so `tests/docs/` can run there once both
branches are merged.
