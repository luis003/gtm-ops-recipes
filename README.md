# gtm-ops-recipes

Small, self-contained scripts for the unglamorous parts of marketing and revenue
operations: auditing an export before you trust it, finding the duplicates your
CRM's native matching misses, catching a broken campaign tag before it ships.

Each recipe is one file. Copy the file, run it, edit it. There is nothing to
install and nothing to configure.

## Principles

- **Standard library only.** Python 3.9+. No `pip install`, no lockfile, no
  virtualenv. If a recipe needs a dependency, it does not belong here.
- **One file per recipe.** Self-contained and readable top to bottom. These are
  meant to be copied into your own repo and changed, not imported as a package.
- **Read-only by default.** Nothing writes to a CRM. Recipes read files, print
  reports, and where useful write a new CSV you asked for.
- **Explain the problem, not just the code.** Every recipe opens with what goes
  wrong in the real world and why the obvious approach fails.
- **Honest about limits.** Where a heuristic is crude, the comment says so.

## Recipes

| Recipe | The problem it solves |
|---|---|
| [`field_audit.py`](recipes/field_audit.py) | Someone hands you a 200-column CRM export and asks if you can segment on it. Prints fill rate, cardinality, inferred type, constant columns, empty columns, and columns holding identical data under different names. |
| [`dedupe_companies.py`](recipes/dedupe_companies.py) | "Acme Inc.", "Acme, Inc", "ACME Incorporated" and acme.com are one account and your CRM thinks they are four. Clusters records by normalized domain, normalized name, and fuzzy name similarity. |
| [`utm_lint.py`](recipes/utm_lint.py) | Your warehouse has `li`, `linkedin`, `LinkedIn`, and `Linkedin` for the same channel, so every dashboard grouping by source is quietly wrong. Validates campaign URLs against a conventions file you control. |

Planned: import preflight (validate a CSV against a target object before load),
email template linting, list hygiene, schema diff between two org configs.

## Usage

Every recipe is a normal script with `--help`.

```bash
# What is actually in this export?
python recipes/field_audit.py contacts.csv

# Only the columns that are more than half populated
python recipes/field_audit.py contacts.csv --min-fill 0.5

# Which accounts are duplicates?
python recipes/dedupe_companies.py accounts.csv --name-col "Account Name" --domain-col Website

# Write the clusters out for review
python recipes/dedupe_companies.py accounts.csv --name-col Name --out clusters.csv

# Are these campaign links tagged correctly?
python recipes/utm_lint.py links.csv --url-col "Landing URL" --conventions conventions.json
```

Recipes that find problems exit with code `2`, so they drop into a CI job or a
scheduled hygiene check without extra glue:

```bash
python recipes/utm_lint.py links.csv --conventions conventions.json || echo "fix the tags"
```

## A note on the defaults

The fuzzy matching threshold in `dedupe_companies.py` and the type inference in
`field_audit.py` are heuristics. They are tuned to be useful on a first run
against a typical B2B CRM export, not to be correct on every dataset. Read the
output before you act on it, and adjust the thresholds to your data rather than
trusting the defaults because they are the defaults.

The same applies to the legal-suffix and generic-domain lists in the dedupe
recipe. They cover the common cases in US and EU B2B data. If you work another
region, add to them.

## Contributing

Issues and pull requests welcome. A good recipe here:

- solves a problem an ops person actually hits, not a hypothetical one
- runs on the standard library
- fits in one readable file
- explains the failure mode it is guarding against

## Who maintains this

Built by Luis Esquivel from a marketing operations seat, mostly by getting
annoyed at doing these things by hand. Also the founder of
[ax1om](https://ax1om.ai), which builds predictive scoring models on a
company's own CRM conversion history.

These recipes are deliberately the boring layer underneath that: data hygiene
and taxonomy work you have to do before any model, including ours, is worth
running. They are useful on their own and carry no dependency on ax1om.

## License

MIT. Use them, change them, ship them in your own repo. Attribution appreciated,
not required.
