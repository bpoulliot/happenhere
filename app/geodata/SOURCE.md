# Bundled US Census Bureau Gazetteer files (item 30)

The offline location lookup (`app/gazetteer.py`) reads two files trimmed from the US Census
Bureau's **2026 Gazetteer Files** (the national files, published September 2026):

| File here | Source file | Columns kept |
|---|---|---|
| `zcta.tsv` | `2026_Gaz_zcta_national.txt` (ZIP Code Tabulation Areas) | ZCTA, latitude, longitude |
| `places.tsv` | `2026_Gaz_place_national.txt` (Places) | GEOID, name, state, latitude, longitude |

* Source page: <https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html>
* Downloads:
  * <https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/2026_Gaz_zcta_national.zip>
    (sha256 `f1e9046b91f6e60686a99343cb2752834c02b0764939403b331c78c017e2c54c`)
  * <https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/2026_Gaz_place_national.zip>
    (sha256 `af678e2d990827c89ee39b98c82de6e90b693c7361ff0e559ae3076670dd2863`)
* **License: public domain** (a work of the US federal government, 17 U.S.C. § 105). Neither
  the ZIP archives (each holds only the one `.txt` file) nor the download page carry a license
  file or statement; the Census Bureau's open-data page says it shares "its public data as open
  data". No attribution is required; this file records the source anyway.
* Trimmed by `scripts/gazetteer_trim.py`: coordinates are the internal point rounded to 4
  decimals (about 11 m); a place name loses its legal-status suffix ("city", "town", "CDP",
  "(balance)"), and one row is kept per state and name (an active government first, then the
  larger land area). 33,791 ZCTAs, 32,139 places.

**Refresh** (each yearly release, a manual rebuild): download the two national ZIP files of
the new vintage, unzip them into an empty directory, run
`python3 -I scripts/gazetteer_trim.py <zcta .txt> <place .txt> app/geodata`, and update
the vintage, URLs, checksums and counts above. Tests never read these files: they use the
synthetic `tests/fixtures/gazetteer`.
