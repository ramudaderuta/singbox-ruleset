# singbox-ruleset

Sing-box rule sets with editable sources, reproducible SRS binaries, and
plain-text DAT exports. Use the files under `srs/` for sing-box subscriptions.

## Repository layout

| Path | Purpose |
| --- | --- |
| [`rules/local/`](rules/local/) | Hand-maintained rules, such as the Steam and Epic download CDNs in `game-cdn.json`. |
| [`rules/upstream/`](rules/upstream/) | Snapshots fetched from upstream; do not edit them by hand. |
| [`rules/overrides/`](rules/overrides/) | Local additions to imported data; edit the FakeIP exceptions here. |
| [`rules/generated/`](rules/generated/) | Combined JSON ready for consumption; built by the same script as the binaries. |
| [`srs/`](srs/) | Published sing-box binary rule sets. |
| [`dat/`](dat/) | Published line-based exports (domain rules and CIDRs, not binary GeoSite files). |
| [`scripts/`](scripts/) | Source refresh, validation, compiler installation, and build. |

The existing SRS and DAT paths stay unchanged, including `srs/remote/{ads,cn,cnip,proxy}.srs`,
`srs/private-ip.srs`, `srs/game-cdn.srs`, and `srs/pt-site.srs`. The old root JSON
files now live under `rules/local/`; the old `remote/*.json` files live under
`rules/upstream/dustinwin/`. `cnc-ipv4` and `globalMedia` are frozen legacy
artifacts: their sources were removed previously and they are not regenerated.

## FakeIP exclusions

- **JSON:** [`rules/generated/fake-ip-filter.json`](rules/generated/fake-ip-filter.json)
- **SRS v2:** [`srs/fake-ip-filter.srs`](srs/fake-ip-filter.srs)
- **Text:** [`dat/fake-ip-filter.dat`](dat/fake-ip-filter.dat)

The build combines the [v2fly NTP](https://github.com/v2fly/domain-list-community/blob/master/data/category-ntp)
and [STUN](https://github.com/v2fly/domain-list-community/blob/master/data/category-stun)
lists, including the NTP regional includes, with locally reviewed LAN suffixes
and exact probe domains for Apple, Android, Windows, and Firefox. The
[v2fly MIT notice](rules/upstream/v2fly/LICENSE) is included. The local probe
selection is intentionally narrower than the
[upstream connectivity-check list](https://github.com/v2fly/domain-list-community/blob/master/data/connectivity-check):
in particular, it does not exclude the whole `www.gstatic.com` host.

In sing-box or Hiddify Core, load this as a rule set and reference it from a
[DNS rule](https://sing-box.sagernet.org/configuration/dns/rule/#rule_set)
that selects the real resolver before FakeIP. Keep static hosts and bootstrap
rules ahead of it. This list does **not** force matching connections to go
`DIRECT` and is independent of `private-ip.srs`.

## Build and refresh

Use Python 3, Git, and Go 1.27.4. The released Hiddify Core CLI does not have
a rule-set compiler command. Instead,
[`install_hiddify_compiler.sh`](scripts/install_hiddify_compiler.sh) builds a
small compiler with the [same sing-box fork revision used by Hiddify Core v5.9.1](https://github.com/hiddify/hiddify-core/tree/v5.9.1/hiddify-sing-box).
It calls the fork's native SRS writer directly and writes SRS v2.

```sh
bash scripts/install_hiddify_compiler.sh /tmp/singbox-ruleset-bin
compiler=/tmp/singbox-ruleset-bin/hiddify-rules-compile

# Regenerate from the checked-in source snapshots.
python3 scripts/update_rules.py --compiler "$compiler"

# Check reproducibility without changing published files.
python3 scripts/update_rules.py --check --compiler "$compiler"

# Fetch changed upstream inputs and rebuild.
python3 scripts/update_rules.py --refresh --compiler "$compiler"
```

The four existing remote JSON rules come from [DustinWin's sing-box rules](https://github.com/DustinWin/ruleset_geodata/tree/sing-box-ruleset).
Refresh resolves each upstream branch once, compares Git blob SHA-1 before
fetching, and checks the size and blob ID of each changed download. It
validates source data and builds the full batch in temporary staging before
any replacement. Only changed files are replaced. An existing SRS v2 file
is kept when its decoded rule payload matches the new output, even if the
compressed bytes differ. This avoids downloads caused only by a compressor change.

One [Rules workflow](.github/workflows/rules.yml) checks builds on pull
requests without write access, builds on pushes to `main`, and refreshes
upstream sources at 19:00 UTC (03:00 Beijing time the next day) or when run
manually on `main`. A successful publishing job commits changed source
snapshots and generated files together; a failed download or build does not
publish them.

