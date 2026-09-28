# Repository count: 13,834 vs 13,631

The 13,631 is the number of repositories that could be **processed**, not the
number in scope. The difference breaks down as follows:

| | Repositories |
|---|---:|
| Your count (threat actor dataset) | 13,834 |
| Repositories in the `github` archive inventory | 13,833 |
| Listed in the inventory but not present in the archive storage | −202 |
| **Repositories analysed** | **13,631** |

## The 202 unavailable repositories

We double-checked all 202 against every storage location holding a copy of
the repositories:

| Location | Path |
|---|---|
| Archive (the copy used for the analysis) | `/data/workarea/archive` |
| Archive in blob storage | `/home/ganeshk/blobcontainer/EYGCO_29062026/AllRepos/AllRepos` |
| Active repositories in blob storage | `/home/ganeshk/blobcontainer/EYGCO_13082026_777Gb/AllRepos` |

For each one we looked for the exact name and for similar spellings (letter
case, dashes and underscores), in its own organisation and in every other
organisation:

- **197 are genuinely absent.** No repository with that name, or a similar
  one, exists in any of the three locations, so there's no data to analyse
  for them.
- **4 exist under a slightly different name.** The inventory spells them
  differently from the actual repository folders:

  | Name in the inventory | Actual repository |
  |---|---|
  | ey-org/evolveagenda-portal | ey-org/evolve-agenda-portal |
  | ey-org/ey-faas-coe | ey-org/eyfaascoe |
  | ey-org/t-o-p-20-risk-enhancement | ey-org/top20riskenhancement |
  | ey-org/unused-c-w-p-g-report-check-tool | ey-org/unused-cwpg-report-check-tool |

  [These were already included in the analysis under their actual names, so
  nothing is missing for them. / These have now been added to the analysis.]

- **1 (`ey-fso-tax/core`)** only matches a repository with the same generic
  name in a different organisation (`ey-org/core`). We believe it's a
  different repository, so it's treated as absent unless you can confirm
  otherwise.

The full list of the 202 repositories, with the result for each, is in
`missing_repos_check.csv` (produced by `check_missing_repos.py`).

## 13,834 vs 13,833

There's a difference of one repository between your count and the inventory.
It's most likely a counting difference, e.g. a repository name that exists
in more than one organisation, or names that differ only in letter case. If
you can share your list of 13,834, we'll match it against ours and confirm
the exact repository.
