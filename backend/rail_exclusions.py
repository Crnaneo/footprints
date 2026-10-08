"""Explicit local display exclusions; never remove or reassign GPS evidence."""
import json
from pathlib import Path


def apply_exclusions(result, directory):
    path=Path(directory)/'excluded-lines.json'
    excluded=json.loads(path.read_text()) if path.exists() else []
    ids={str(row['line_id']) for row in excluded}
    return {**result,'manual_exclusions':excluded,
        **{key:[row for row in result.get(key,[]) if str(row['line_id']) not in ids]
           for key in ('lines','unresolved_lines','shared_only_lines')}}
