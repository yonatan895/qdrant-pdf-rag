"""Open WebUI Kind trial (issue #1073): summarise the node egress audit.

    nft -j list set inet owui_audit flows | python3 egress_report.py flows IPS_FILE
    kubectl -n kube-system logs -l k8s-app=kube-dns | python3 egress_report.py dns IPS_FILE

`flows` prints every distinct (destination, protocol, port) first opened by
one of the Open WebUI pod IPs in IPS_FILE; `dns` prints every name those IPs
asked CoreDNS for, with counts. Addresses, names and counts only.
"""
from __future__ import annotations

import collections
import json
import re
import sys

DNS_LINE = re.compile(r'\] (\d+\.\d+\.\d+\.\d+):\d+ - \d+ "(\w+) IN (\S+)')


def _concat(elem):
    value = elem["elem"]["val"] if isinstance(elem, dict) and "elem" in elem else elem
    return tuple(str(x) for x in value["concat"])


def main(argv: list[str]) -> int:
    mode, ips_file = argv
    with open(ips_file, encoding="utf-8") as fh:
        ips = set(fh.read().split())
    if mode == "flows":
        doc = json.load(sys.stdin)["nftables"]
        elems = next(item["set"] for item in doc if "set" in item).get("elem", [])
        rows = sorted({_concat(e) for e in elems})
        for row in rows:
            if row[0] in ips:
                print(*row)
        print("total distinct flows, all pods:", len(rows))
    else:
        counts = collections.Counter()
        for line in sys.stdin:
            match = DNS_LINE.search(line)
            if match and match.group(1) in ips:
                counts[(match.group(3), match.group(2))] += 1
        for (name, qtype), n in sorted(counts.items()):
            print(n, name, qtype)
        print("total queries:", sum(counts.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
