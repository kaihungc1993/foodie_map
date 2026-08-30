"""Repoint notes at renamed restaurant ids.

docs/data/notes.json is keyed on restaurant id. Ids are meant to be permanent,
but one can retire — a venue known only from a roundup mention gains its own
review post and moves to a better key. build.py records every such move in
`id_aliases`; this applies them so a note follows its restaurant.

Safe to run repeatedly: it only ever adds a key that is missing.
"""
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import common


def main():
    data = common.read_json(common.RESTAURANTS, {})
    aliases = data.get("id_aliases", {})
    notes = common.read_json(common.NOTES, {})
    live = {r["id"] for r in data.get("restaurants", [])}

    moved, orphaned = [], []
    for old_id, note in list(notes.items()):
        if old_id in live:
            continue
        target = aliases.get(old_id)
        if target and target in live:
            if target not in notes:
                notes[target] = note
                moved.append((old_id, target))
        else:
            orphaned.append(old_id)

    if moved:
        common.write_json(common.NOTES, notes)
    print(f"{len(notes)} notes, {len(moved)} repointed, {len(orphaned)} with no target")
    for a, b in moved:
        print(f"  {a} -> {b}")
    for o in orphaned:
        print(f"  WARNING: note on {o} has no live restaurant", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
