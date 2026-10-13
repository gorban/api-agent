"""Compact coverage disclosure and durable, local drill-down artifacts."""
from dataclasses import dataclass
from pathlib import Path
import shutil


@dataclass(frozen=True)
class CoverageReport:
    repo: str
    json_path: Path
    html_path: Path


def retain_report(repo, temporary, destination):
    """Keep complete evidence after the runner's scratch directory is removed."""
    temporary, destination = Path(temporary), Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for name in ('coverage.json', '.coverage'):
        shutil.copy2(temporary / name, destination / name)
    shutil.copytree(temporary / 'html', destination / 'html')
    return CoverageReport(repo, destination / 'coverage.json', destination / 'html/index.html')


def _percent(value, complete):
    shown = f'{value:.2f}%'
    return '<100%' if not complete and shown == '100.00%' else shown


def coverage_table(areas, *, full=False):
    """Show exact gaps once, with a bounded preview unless full output is chosen."""
    incomplete = [area for area in areas if not area.complete]
    if not areas:
        return []
    if not incomplete and not full:
        return ['Code coverage: lines 100% · branches 100%.']
    selected = list(areas) if full else incomplete[:20]
    names = [f'{area.repo}::{area.file}' for area in selected]
    width = max(len('File'), *(len(name) for name in names))
    if not full:
        width = min(80, width)
    lines = [f'{"File":<{width}}  lines    branches  Missing']
    for name, area in zip(names, selected):
        shown = name if len(name) <= width else name[:width - 1] + '…'
        missing = ', '.join(str(line) for line in area.missing_lines)
        branches = ', '.join(f'{start}→{end}' for start, end in area.missing_branches)
        details = '; '.join(part for part in (missing, 'branches ' + branches if branches else '') if part) or '—'
        if not full and len(details) > 60:
            details = details[:59] + '…'
        line_percent = _percent(area.line_percent, area.covered_lines == area.num_statements)
        branch_percent = _percent(area.branch_percent, area.covered_branches == area.num_branches)
        lines.append(f'{shown:<{width}}  {line_percent:>7}  {branch_percent:>7}  {details}')
    if len(selected) < len(incomplete):
        lines.append(f'… {len(incomplete) - len(selected)} more files below 100%; '
                     f'{len(areas) - len(incomplete)} files fully covered.')
    else:
        lines.append(f'Coverage: {len(incomplete)} files below 100%; '
                     f'{len(areas) - len(incomplete)} files fully covered.')
    return lines
