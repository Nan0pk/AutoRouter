import argparse
import json
import sys
from pathlib import Path

from .hermes import discover
from .install import install, status, undo
from .policy import MODES, normalize_mode, select_models


def choose(prompt, values, reader=input):
    if not values:
        raise ValueError("No connected providers or eligible choices found; sign in with the harness first")
    for number, value in enumerate(values, 1):
        print(f"  {number}. {value}")
    answer = reader(prompt).strip()
    if not answer:
        return values[0]
    if answer.isdigit() and 1 <= int(answer) <= len(values):
        return values[int(answer)-1]
    if answer in values:
        return answer
    raise ValueError("Choose an item from the displayed list")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Set up usable AutoRouter routing in a supported harness.")
    parser.add_argument("command", choices=("setup", "inspect", "status", "verify", "undo"))
    parser.add_argument("harness")
    parser.add_argument("--home", type=Path, help="Hermes profile directory (defaults to the current Hermes home)")
    parser.add_argument("--mode", type=normalize_mode, choices=MODES,
                        help="all includes subscriptions and verified free models; subscriptions remains an alias for all")
    parser.add_argument("--provider", action="append", default=[])
    parser.add_argument("--model", action="append", default=[])
    parser.add_argument("--confirm-allowance", action="append", default=[], help="Record your assertion that this provider has included allowance")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--offline", action="store_true", help="Configure/check locally, leaving live verification explicitly incomplete")
    parser.add_argument("--inventory", type=Path, help="Dry-run-only inventory fixture; never authorizes installation")
    args = parser.parse_args(argv)
    try:
        if args.harness.lower() != "hermes":
            raise ValueError(f"{args.harness} setup is not supported yet. The supported target is Hermes.")
        if args.inventory and not args.dry_run:
            raise ValueError("--inventory is allowed only with --dry-run")
        import os
        home = args.home or Path(os.environ.get("HERMES_HOME", Path.home()/".hermes"))
        if args.command == "status":
            print(json.dumps(status(home), indent=2)); return 0
        if args.command == "undo":
            print(json.dumps(undo(home), indent=2)); return 0
        if args.command == "verify":
            from .verify import verify_install
            result = verify_install(home, live=not args.offline)
            print(json.dumps(result, indent=2)); return 0 if result.get("handoff_verified") else 1
        interactive = sys.stdin.isatty()
        if args.command == "setup" and not interactive and not args.dry_run and (not args.mode or not args.yes):
            raise ValueError("Noninteractive setup needs --mode and --yes; use --dry-run to preview without changes")
        inventory = json.loads(args.inventory.read_text()) if args.inventory else discover(home)
        if args.command == "inspect":
            print(json.dumps(inventory, indent=2)); return 0
        mode = args.mode
        if mode is None and interactive:
            labels = ["AutoRouter-All — subscriptions and free models", "One provider", "Pick individual models", "Free models only"]
            choice = choose("Choose model sources [1]: ", labels)
            mode = MODES[labels.index(choice)]
        mode = mode or "all"
        providers = args.provider; models = args.model
        if mode == "provider" and not providers and interactive:
            providers = [choose("Choose provider: ", [p["id"] for p in inventory["providers"]])]
        if mode == "models" and not models and interactive:
            for row in inventory["models"]:
                print(f"  {row['id']} — {row['billing']}")
            models = [v.strip() for v in input("Choose exact model IDs, separated by commas: ").split(",") if v.strip()]
        policy = select_models(inventory, mode, providers, models, args.confirm_allowance)
        if not args.dry_run and not args.offline:
            from .probe import probe_model, select_usable
            policy = select_usable(inventory, mode, providers, models, args.confirm_allowance,
                                   probe=lambda row: probe_model(row, home=home))
        print(f"Profile: {policy['name']}\nRoutine: {policy['routine']}\nSpecialist: {policy['specialist']}")
        for reason in policy["reasons"]:
            print(reason)
        print("Selected model quota:")
        for identifier in (policy["routine"], policy["specialist"]):
            row = next(r for r in policy["models"] if r["id"] == identifier)
            quota = row.get("quota")
            windows = (quota or {}).get("windows", [])
            print(f"  {identifier}: " + (", ".join(f"{w.get('name', 'window')} {w.get('used_percent', 'unknown')}% used" for w in windows) if windows else "unknown"))
        if args.dry_run:
            print("Dry-run: no configuration changed and no inference requested."); return 0
        install(home, policy)
        if args.offline:
            print("Installed locally; live routing and handoff are not verified."); return 0
        from .verify import verify_install
        result = verify_install(home, live=True)
        print(json.dumps(result, indent=2))
        return 0 if result.get("handoff_verified") else 1
    except (ValueError, OSError, EOFError) as exc:
        print(f"AutoRouter: {exc}", file=sys.stderr)
        return 2
