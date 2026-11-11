"""五项优先实测的只读盘点及独立全文件哈希入口。"""
import argparse
import asyncio
from pathlib import Path

from priority_harness.inventory import prepare
from priority_harness.integrity import run
from priority_harness.observer import observe
from priority_harness.review import run as review
from priority_harness.publication import run as publication, staging_faults
from priority_harness.endpoint import run as endpoint
from priority_harness.protection import run as protection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--hash', action='store_true')
    parser.add_argument('--suite', type=Path)
    parser.add_argument('--observe-server', action='store_true')
    parser.add_argument('--observe-dom', action='store_true')
    parser.add_argument('--observe-replies', action='store_true')
    parser.add_argument('--observe-latest', action='store_true')
    parser.add_argument('--groups', nargs='+', type=int, default=[])
    parser.add_argument('--review', action='store_true')
    parser.add_argument('--publication', action='store_true')
    parser.add_argument('--staging-faults', action='store_true')
    parser.add_argument('--runtime-suite', type=Path)
    parser.add_argument('--deep-endpoint', action='store_true')
    parser.add_argument('--check-source', action='store_true')
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    if args.prepare:
        suite = prepare(project)
    elif args.suite:
        suite = args.suite.resolve()
    else:
        parser.error('需要 --prepare 或 --suite')
    if not suite.is_relative_to(project / '~temp/priority-live'):
        parser.error('只允许使用项目隔离实测批次')
    if args.hash:
        raise SystemExit(run(suite))
    if args.check_source:
        raise SystemExit(protection(suite))
    if args.publication or args.staging_faults or args.deep_endpoint:
        runtime = args.runtime_suite.resolve() if args.runtime_suite else None
        if runtime is None or not runtime.is_relative_to(project / '~temp/remaining-live'):
            parser.error('--runtime-suite 必须是已准备的剩余实测隔离批次')
        codes = []
        if args.publication:
            codes.append(publication(project, runtime))
        if args.staging_faults:
            codes.append(staging_faults(project, runtime))
        if args.deep_endpoint:
            codes.append(endpoint(project, runtime))
        raise SystemExit(max(codes))
    if args.review:
        raise SystemExit(review(suite))
    if args.observe_server or args.observe_dom or args.observe_replies or args.observe_latest:
        raise SystemExit(asyncio.run(observe(suite, server=args.observe_server,
                                             dom=args.observe_dom, replies=args.observe_replies,
                                             latest=args.observe_latest,
                                             numbers=args.groups)))


if __name__ == '__main__':
    main()
