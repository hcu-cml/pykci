# Round-trip census statistics

Backing data for the two census tables in the paper (Chapter 5,
`subsec:census`). All figures are produced by `eval/roundtrip_census.py`,
comparing each **source** CityGML file against its **exported** counterpart at
four levels (element local-names, `(attribute, value)`, `(element, text)`,
coordinate tokens). The appearance/material module is out of scope and excluded
from every count.

- `census_fzkhaus.json` — LoD axis (FZK-Haus LoD0–4) → `tab:census`
- `census_railway.json` — thematic-module axis (Railway Scene LoD3) → `tab:census_railway`

`missing_final` and `missing` were verified at **0** against the exports on disk.
The FZK `missing_naive` column is the documented pre-fix naive-mapping baseline
(from the paper); it is not re-derivable now because the naive mapping was
replaced by the fidelity-preserving one.

---

## Table 1 — FZK-Haus, by Level of Detail (`tab:census`)

| LoD | Dominant new construct | Source instances | Missing (naive) | Missing (final) |
| --- | --- | ---: | ---: | ---: |
| 0 | footprint, roof edge, address      | 117     | 79      | **0** |
| 1 | lod1Solid                          | 193     | 145     | **0** |
| 2 | typed boundary surfaces            | 276     | 53      | **0** |
| 3 | openings, composite surfaces       | 5,306   | 828     | **0** |
| 4 | full interior, orientable surfaces | 275,366 | 270,887 | **0** |

Caption figures: a naive mapping loses up to `\num{270887}` instances at LoD4;
the final mapping is missing `\num{0}` at every level (`\qty{100}{\percent}`
coverage).

## Table 2 — Railway Scene LoD3, by CityGML module (`tab:census_railway`)

52 top-level features across 10 modules (LandUse, also supported, does not occur
in this scene).

| Module | Features | Distinctive construct exercised | Missing |
| --- | ---: | --- | ---: |
| Building          | 3  | LoD3 openings, boundary surfaces, installations | **0** |
| Bridge            | 4  | nested construction elements, installations     | **0** |
| Tunnel            | 4  | nested installations, closure surfaces          | **0** |
| Transportation    | 10 | surface-only LoD geometry (Railway)             | **0** |
| Vegetation        | 15 | solitary objects, lodN geometry                 | **0** |
| CityFurniture     | 11 | lodN geometry                                   | **0** |
| WaterBody         | 1  | water boundary surfaces                         | **0** |
| Relief            | 1  | native TIN (TriangulatedSurface)                | **0** |
| CityObjectGroup   | 1  | XLink group membership                          | **0** |
| GenericCityObject | 2  | typed generic attributes                        | **0** |
| **Total**         | **52** | ten CityGML modules                         | **0** |

Census totals (source instances, all reproduced in the export):

| Level | Source instances | Missing |
| --- | ---: | ---: |
| elements    | 243,254   | 0 |
| attributes  | 487       | 0 |
| texts       | 1,194     | 0 |
| coordinates | 937,167   | 0 |
| **Total**   | **1,182,102** | **0** |

Caption figures: covers all `\num{1182102}` source instances (`\num{243254}`
elements, `\num{487}` attributes, `\num{1194}` text values, `\num{937167}`
coordinate tokens) with `\num{0}` missing (`\qty{100}{\percent}` coverage).
