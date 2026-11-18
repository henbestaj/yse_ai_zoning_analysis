# Zoning Results Viewer

A website for reading and checking zoning results: plain HTML, CSS and JavaScript, with no build step. It
shows the JSON files that `zoning_db.py export --format json` writes, with the quote and page behind every
value. GitHub Pages hosts it, and the results come from the repo's releases, so no data is committed.

| File | What it does |
|---|---|
| `index.html`, `style.css`, `app.js` | The viewer |
| `build_index.py` | Lists the published runs in `data/index.json`, which the viewer reads |
| `../.github/workflows/pages.yml` | Gathers the viewer and the release files, then deploys them to Pages |

## What reviewers get

- **Districts:** one district at a time. Values that apply generally are in bold, and conditional ones show
  their condition. Click a value for its quote, a *Copy quote* button (to search for it in the PDF), and links
  that open the PDF at that page. *To check* hides fields that have a clean general value.
- **Compare a field:** one requirement across every district, with bars and sorting for numbers, so odd values
  stand out.
- **Table:** every district and field at once, colored by status. Click a cell to jump to it.
- Marks show each field's status: has a value, take a closer look (only conditional values, several values,
  or a flag), doesn't apply, and not found.
- The address bar always points at the current view (run, district, field), so a link can be sent to a
  teammate. *Copy link* buttons do the same. Keyboard: ← → for the previous or next district or field, and
  / to search.

## One-time setup

1. Pages needs a public repo or GitHub Pro. Pro is free for students through GitHub Education. **The site is
   public either way**: anyone with its address can open it, even though the repo stays private.
2. In the repo on GitHub: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
3. Push `site/` and `.github/workflows/pages.yml` to `main`. That push deploys the viewer. The address is
   shown in Settings → Pages, and on each run in the **Actions** tab.

## Publishing runs

Export each run with its zoning document, then attach both files to a release:

```
uv run python zoning_db.py export --run 3 --format json --doc "path/to/Darien Zoning Ordinance.pdf"
uv run python zoning_db.py export --town new_haven --format json      # a town's PDF is found on its own
gh release create results-2026-11-17 --title "Darien and New Haven" --notes "First batch" *_zones.json *_document.pdf
```

Creating the release (or publishing a draft) starts the workflow, which deploys within a minute or two. You
can also create the release on the GitHub website (**Releases → Draft a new release**) and drag the files in.

- **Every release counts.** The site shows the runs from all releases, so a new release adds to what's there.
  Several runs of the same place show up under one place, with the newest first.
- **The PDF is optional.** Without it, page numbers show as plain text instead of links. `--doc` checks that
  the PDF is the exact file the run used.
- **Removing runs:** delete the release, which redeploys automatically. Adding or deleting single files on an
  existing release doesn't start a deploy, so after that run it by hand: **Actions → Publish results viewer →
  Run workflow**, or `gh workflow run pages.yml`.

## Checking a run without publishing it

Open the site (or `index.html` straight from disk) and drop the `.json` file onto the page, together with
its PDF if you want page links. The files stay in your browser; nothing is uploaded. The PDF can be the
`_document.pdf` copy or the original. Either way, the viewer checks it against the run's fingerprint and
won't link to a different document.

## Previewing the site locally

```
mkdir site/data                       # ignored by git
# copy some *_zones.json and *_document.pdf exports into site/data, then:
uv run python site/build_index.py site/data
uv run python -m http.server 8000 -d site
```

Then open http://localhost:8000.

## Changing the viewer

Edit the three files and refresh. Everything on screen comes from the JSON format documented in
`export_json` in `zoning_db.py`. If that format changes in a way old files can't follow, bump its `version`,
and update `prepare()` in `app.js` and the version check in `build_index.py` to match.
