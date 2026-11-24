import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
LOCAL = Path("rules/local")
MOSDNS_LOCAL = {LOCAL / "locals.json", LOCAL / "shs.json"}
DNS_FIELDS = {"domain", "domain_suffix", "domain_regex"}
DUSTIN = Path("rules/upstream/dustinwin")
FAKE_IP = LOCAL / "fake-ip-filter.json"
REMOTE_RULES = ("ads", "cn", "cnip", "proxy")
LEGACY_NAMES = {Path("cnc-ipv4"), Path("globalMedia")}
DAT_FIELDS = {
    "domain": "full:",
    "domain_suffix": "domain:",
    "domain_keyword": "keyword:",
    "domain_regex": "regexp:",
    "ip_cidr": "",
}
MAX_DOWNLOAD = 16 * 1024 * 1024
SHA1 = re.compile(r"[0-9a-f]{40}")
SRS_V2 = b"SRS\x02"
MAX_SRS_PAYLOAD = 64 * 1024 * 1024


def download(url):
    with urlopen(Request(url, headers={"User-Agent": "singbox-ruleset-refresh"}), timeout=25) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if not data or len(data) > MAX_DOWNLOAD:
        raise ValueError(f"invalid download size: {url}")
    return data


def git_blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def refresh_sources(repo, branch, paths):
    api = f"https://api.github.com/repos/{repo}"
    ref = json.loads(download(f"{api}/git/ref/heads/{branch}"))["object"]["sha"]
    if not SHA1.fullmatch(ref):
        raise ValueError(f"{repo}: invalid commit SHA")
    tree = json.loads(download(f"{api}/git/trees/{ref}?recursive=1"))
    if tree["truncated"] is not False:
        raise ValueError(f"{repo}: incomplete tree")
    entries = {item["path"]: item for item in tree["tree"]}
    changes = {}
    for remote, local in paths.items():
        entry = entries.get(remote, {})
        sha = entry.get("sha", "")
        size = entry.get("size", 0)
        if entry.get("type") != "blob" or entry.get("mode") != "100644" or not SHA1.fullmatch(sha) or not 0 < size <= MAX_DOWNLOAD:
            raise ValueError(f"{repo}: missing or invalid source {remote}")
        existing = ROOT / local
        if existing.is_file() and git_blob_sha(existing.read_bytes()) == sha:
            continue
        data = download(f"https://raw.githubusercontent.com/{repo}/{ref}/{remote}")
        if len(data) != size or git_blob_sha(data) != sha:
            raise ValueError(f"{repo}: invalid blob {remote}")
        changes[local] = data
    print(f"{repo}@{ref}: {len(changes)} of {len(paths)} source files downloaded")
    return changes


def source_bytes(path, overrides):
    return overrides[path] if path in overrides else (ROOT / path).read_bytes()


def rules_json(data, path, allowed=DAT_FIELDS):
    doc = json.loads(data)
    if not isinstance(doc, dict) or type(doc.get("version")) is not int or not 1 <= doc["version"] <= 5:
        raise ValueError(f"{path}: unsupported source version")
    rules = doc.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError(f"{path}: no rules")
    for rule in rules:
        if not isinstance(rule, dict) or not rule or any(key not in allowed for key in rule):
            raise ValueError(f"{path}: unsupported rule fields")
        for key, values in rule.items():
            if not isinstance(values, list) or not values or any(
                not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value or "\0" in value
                for value in values
            ):
                raise ValueError(f"{path}: invalid {key} values")
    return doc


def validate_fake_ip(doc):
    domains = {value.lower() for rule in doc["rules"] for value in rule.get("domain", [])}
    suffixes = {value.lower() for rule in doc["rules"] for value in rule.get("domain_suffix", [])}
    for name in ("www.gstatic.com", "www.baidu.com", "www.bilibili.com"):
        if name in domains or any(name == suffix or name.endswith("." + suffix.lstrip(".")) for suffix in suffixes):
            raise ValueError(f"FakeIP exclusions must not match {name}")


def dat_bytes(doc):
    lines = {
        prefix + (value.lstrip(".") if prefix else value)
        for rule in doc["rules"]
        for key, prefix in DAT_FIELDS.items()
        for value in rule.get(key, [])
    }
    return ("\n".join(sorted(lines)) + "\n").encode()


def dns_values(values, path):
    if (not isinstance(values, list) or not values or
            any(not isinstance(s, str) or not s or
                "#" in s or "\0" in s or any(c.isspace() for c in s) for s in values)):
        raise ValueError(f"{path}: invalid mosdns matcher values")
    return values


def dns_leaf(rule, fields, path):
    if not isinstance(rule, dict) or not rule or not rule.keys() <= fields:
        raise ValueError(f"{path}: unexpected matcher structure in local DNS export")
    for key, values in rule.items():
        if key == "invert":
            if values is not True:
                raise ValueError(f"{path}: expected an inverted matcher")
        else:
            for value in dns_values(values, path):
                if key in {"ip_cidr", "source_ip_cidr"}:
                    ipaddress.ip_network(value)


def dns_guard(rule, path):
    if isinstance(rule, dict) and rule.get("type") == "logical":
        if (rule.keys() - {"invert"} != {"type", "mode", "rules"} or
                rule["mode"] not in {"and", "or"} or
                ("invert" in rule and rule["invert"] is not True) or
                not isinstance(rule["rules"], list) or not rule["rules"]):
            raise ValueError(f"{path}: unexpected logical guard in local DNS export")
        for child in rule["rules"]:
            dns_guard(child, path)
    else:
        dns_leaf(rule, {"ip_cidr", "source_ip_cidr", "invert"}, path)


def mosdns_dat_bytes(data, path):
    """Export positive qnames; both outputs feed the same DNS resolver."""
    doc = json.loads(data)
    if (not isinstance(doc, dict) or doc.get("version") != 5 or
            not isinstance(doc.get("rules"), list) or not doc["rules"]):
        raise ValueError(f"{path}: expected a nonempty v5 local rule set")
    lines = set()
    for rule in doc["rules"]:
        if not isinstance(rule, dict):
            raise ValueError(f"{path}: invalid rule")
        primary, guards = rule, []
        if rule.get("type") == "logical":
            if (rule.keys() != {"type", "mode", "rules"} or rule["mode"] != "and" or
                    not isinstance(rule["rules"], list) or len(rule["rules"]) < 2):
                raise ValueError(f"{path}: expected a destination followed by guards")
            primary, *guards = rule["rules"]
        dns_leaf(primary, DNS_FIELDS | {"ip_cidr", "source_ip_cidr"}, path)
        is_ip = "ip_cidr" in primary
        if is_ip and (primary.keys() & DNS_FIELDS or
                      {"domain_regex": [".+"], "invert": True} not in guards):
            raise ValueError(f"{path}: expected an IP-only destination")
        if not is_ip and not primary.keys() & DNS_FIELDS:
            raise ValueError(f"{path}: no destination matcher")
        for guard in guards:
            if isinstance(guard, dict) and guard.get("invert") is True and guard.keys() & DNS_FIELDS:
                dns_leaf(guard, DNS_FIELDS | {"invert"}, path)
            else:
                dns_guard(guard, path)
        if is_ip:
            continue
        lines.update("full:" + name for name in primary.get("domain", []))
        lines.update("regexp:" + pattern for pattern in primary.get("domain_regex", []))
        for suffix in primary.get("domain_suffix", []):
            # mosdns domain: includes the root; a leading-dot sing-box suffix does not.
            lines.add("regexp:" + re.escape(suffix) + "$" if suffix.startswith(".") else "domain:" + suffix)
    if not lines:
        raise ValueError(f"{path}: no qnames to export")
    return ("\n".join(sorted(lines)) + "\n").encode()


def build_mosdns_local(stage):
    outputs = {}
    for path in sorted(MOSDNS_LOCAL):
        target = Path("dat") / path.with_suffix(".dat").name
        candidate = stage / target
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(mosdns_dat_bytes((ROOT / path).read_bytes(), path))
        outputs[target] = candidate
    return outputs


def build(stage, updates, compiler):
    outputs = {}
    inputs = {
        file.relative_to(ROOT / LOCAL).with_suffix(""): file.relative_to(ROOT)
        for file in (ROOT / LOCAL).rglob("*.json")
    }
    extra = {Path("remote") / name: DUSTIN / f"{name}.json" for name in REMOTE_RULES}
    if inputs.keys() & (extra.keys() | LEGACY_NAMES):
        raise ValueError("local source conflicts with a reserved rule name")
    inputs.update(extra)

    for name, path in sorted(inputs.items()):
        data = source_bytes(path, updates)
        if path in MOSDNS_LOCAL:
            text_data = mosdns_dat_bytes(data, path)
        elif path == FAKE_IP:
            doc = rules_json(data, path, allowed=("domain", "domain_suffix"))
            validate_fake_ip(doc)
            text_data = dat_bytes(doc)
        else:
            text_data = dat_bytes(rules_json(data, path))
        source = stage / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(data)

        target = Path("srs") / name.with_suffix(".srs")
        binary = stage / target
        binary.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([compiler, "rule-set", "compile", str(source), "-o", str(binary)], check=True)
        payload = binary.read_bytes()
        if len(payload) <= 4 or payload[:4] != SRS_V2:
            raise ValueError(f"{target}: expected SRS format v2")
        outputs[target] = binary

        target = Path("dat") / name.with_suffix(".dat")
        text = stage / target
        text.parent.mkdir(parents=True, exist_ok=True)
        text.write_bytes(text_data)
        outputs[target] = text

    for path in (Path("srs"), Path("dat")):
        unmanaged = {file.relative_to(ROOT) for file in (ROOT / path).rglob(f"*.{path.name}")} - outputs.keys()
        if any(file.relative_to(path).with_suffix("") not in LEGACY_NAMES for file in unmanaged):
            raise ValueError(f"{path}: remove or explicitly retain orphaned artifacts: {sorted(unmanaged)}")
    return outputs


def srs_payload(data):
    stream = zlib.decompressobj()
    payload = stream.decompress(data[len(SRS_V2):], MAX_SRS_PAYLOAD)
    if not stream.eof or stream.unused_data or stream.unconsumed_tail:
        raise ValueError("invalid or oversized SRS payload")
    return payload


def matches(path, candidate):
    if not path.is_file():
        return False
    current, built = path.read_bytes(), candidate.read_bytes()
    if current == built:
        return True
    if path.suffix != ".srs" or not (current.startswith(SRS_V2) and built.startswith(SRS_V2)):
        return False
    try:
        return srs_payload(current) == srs_payload(built)
    except (ValueError, zlib.error):
        return False


def main():
    parser = argparse.ArgumentParser(description="Refresh sources and build sing-box rule sets.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--refresh", action="store_true", help="Fetch changed upstream blobs before building.")
    modes.add_argument("--check", action="store_true", help="Build offline and fail if committed outputs would change.")
    parser.add_argument("--mosdns-local", action="store_true", help="Export only the two mosdns qname lists.")
    parser.add_argument("--compiler", default="hiddify-rules-compile", help="Path to the rules compiler.")
    args = parser.parse_args()
    if args.mosdns_local and args.refresh:
        parser.error("--mosdns-local cannot be combined with --refresh")
    updates = {}
    if args.refresh:
        updates = refresh_sources(
            "DustinWin/ruleset_geodata", "sing-box-ruleset",
            {f"{name}.json": DUSTIN / f"{name}.json" for name in REMOTE_RULES},
        )

    with TemporaryDirectory(prefix=".rule-build-", dir=ROOT) as directory:
        stage = Path(directory)
        outputs = build_mosdns_local(stage) if args.mosdns_local else build(stage, updates, args.compiler)
        for name, data in updates.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            outputs[name] = target
        changes = {
            name: candidate
            for name, candidate in outputs.items()
            if not matches(ROOT / name, candidate)
        }
        if args.check and changes:
            parser.exit(1, "Out of date: " + ", ".join(str(name) for name in sorted(changes)) + "\n")
        for name, candidate in sorted(changes.items()):
            dest = ROOT / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(candidate, dest)
            print(f"Updated {name}")
        print(f"Built {len(outputs) - len(updates)} artifacts; {len(changes)} changed.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
