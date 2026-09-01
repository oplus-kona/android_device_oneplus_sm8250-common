#!/usr/bin/env python3
"""
clean_vendor_blobs.py

Automatically removes deleted proprietary blobs from vendor tree (physical files,
<device>-vendor.mk, and Android.bp) based on changes in proprietary-files.txt.
"""

import sys
import os
import re
import argparse
import subprocess
from pathlib import Path


def parse_proprietary_line(line: str) -> str:
    """Extracts the file path from a proprietary-files.txt line."""
    line = line.strip()
    if not line or line.startswith('#'):
        return None
    # Handle flags (;FLAG), hashes (|hash), destination remaps (:dest)
    path_part = line.split(';')[0].split('|')[0].split(':')[0].strip()
    if path_part.startswith('-'):
        path_part = path_part[1:]
    return path_part


def get_deleted_files_from_git(device_dir: Path, rev: str = None) -> list:
    """Gets deleted proprietary file paths from git diff in device repo."""
    cmd = ['git', '-C', str(device_dir), 'diff']
    if rev:
        if '..' in rev or '~' in rev or '^' in rev:
            cmd.extend([rev, '--', 'proprietary-files.txt'])
        else:
            cmd.extend([f'{rev}^..{rev}', '--', 'proprietary-files.txt'])
    else:
        cmd.extend(['--', 'proprietary-files.txt'])

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        # Try diff against HEAD if unstaged is empty
        cmd = ['git', '-C', str(device_dir), 'diff', 'HEAD~1..HEAD', '--', 'proprietary-files.txt']
        res = subprocess.run(cmd, capture_output=True, text=True)

    deleted = []
    for line in res.stdout.splitlines():
        if line.startswith('-') and not line.startswith('---'):
            parsed = parse_proprietary_line(line[1:])
            if parsed:
                deleted.append(parsed)
    return deleted


def remove_block_from_android_bp(bp_path: Path, module_names: set, file_paths: set) -> int:
    """Removes prebuilt module blocks from Android.bp that match module_name or file_path."""
    if not bp_path.exists():
        return 0

    content = bp_path.read_text()
    
    # Match top-level blocks: <type> { ... }
    block_pattern = re.compile(
        r'([a-zA-Z0-9_]+)\s*\{\s*\n(.*?)\n\}\n*',
        re.DOTALL
    )

    removed_count = 0

    def should_remove_block(block_type: str, block_body: str) -> bool:
        # Check name
        name_match = re.search(r'name:\s*"([^"]+)"', block_body)
        if name_match:
            name = name_match.group(1)
            if name in module_names:
                return True
        
        # Check srcs
        for fp in file_paths:
            if f'proprietary/{fp}' in block_body:
                return True
        return False

    # Filter out matched blocks
    new_blocks = []
    last_idx = 0
    for match in block_pattern.finditer(content):
        b_type = match.group(1)
        b_body = match.group(2)
        
        if should_remove_block(b_type, b_body):
            # Include text before block
            new_blocks.append(content[last_idx:match.start()])
            last_idx = match.end()
            removed_count += 1
        else:
            new_blocks.append(content[last_idx:match.end()])
            last_idx = match.end()

    new_blocks.append(content[last_idx:])
    new_content = "".join(new_blocks)
    
    # Clean up double blank lines
    new_content = re.sub(r'\n{3,}', '\n\n', new_content)
    bp_path.write_text(new_content)
    return removed_count


def remove_entries_from_vendor_mk(mk_path: Path, file_paths: set, module_names: set) -> tuple:
    """Removes file copies and packages from <device>-vendor.mk."""
    if not mk_path.exists():
        return 0, 0

    lines = mk_path.read_text().splitlines()
    new_lines = []
    copy_removed = 0
    pkg_removed = 0

    in_copy_files = False
    in_packages = False

    for i, line in enumerate(lines):
        # Detect sections
        if line.strip().startswith('PRODUCT_COPY_FILES'):
            in_copy_files = True
        elif line.strip().startswith('PRODUCT_PACKAGES'):
            in_packages = True
        elif line.strip().startswith('PRODUCT_') or line.strip().startswith('$(') or not line.strip():
            if not line.strip().endswith('\\'):
                in_copy_files = False
                in_packages = False

        stripped = line.strip()
        matched = False

        if in_copy_files:
            for fp in file_paths:
                if f'proprietary/{fp}:' in stripped:
                    matched = True
                    copy_removed += 1
                    break

        if in_packages and not matched:
            # Check package names: "libxyz \" or "libxyz"
            pkg_name = stripped.rstrip('\\').strip()
            if pkg_name in module_names:
                matched = True
                pkg_removed += 1

        if not matched:
            new_lines.append(line)

    # Fix trailing backslashes for blocks if last item was removed
    fixed_lines = []
    for i, line in enumerate(new_lines):
        # If the next line is empty/non-continuation and current line ends with '\', check if it's the last in block
        fixed_lines.append(line)

    mk_path.write_text('\n'.join(fixed_lines) + '\n')
    return copy_removed, pkg_removed


def main():
    parser = argparse.ArgumentParser(description="Clean up vendor tree from deleted proprietary-files.")
    parser.add_argument("--device-dir", "-d", default=".", help="Path to device tree directory")
    parser.add_argument("--vendor-dir", "-v", default=None, help="Path to vendor directory (auto-detected if omitted)")
    parser.add_argument("--commit", "-c", default=None, help="Git commit or diff range in device tree")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Print what would be deleted without modifying files")
    args = parser.parse_args()

    device_dir = Path(args.device_dir).resolve()
    
    # Auto-detect vendor dir if not specified
    vendor_dir = args.vendor_dir
    if not vendor_dir:
        # Check standard android paths: device/oneplus/sm8250-common -> vendor/oneplus/sm8250-common
        parts = device_dir.parts
        if 'device' in parts:
            idx = parts.index('device')
            rel_parts = parts[idx+1:]
            candidate = Path(*parts[:idx]) / 'vendor' / Path(*rel_parts)
            if candidate.exists():
                vendor_dir = candidate

    if not vendor_dir or not Path(vendor_dir).exists():
        print(f"Error: Could not locate vendor directory: {vendor_dir}", file=sys.stderr)
        sys.exit(1)

    vendor_dir = Path(vendor_dir).resolve()
    print(f"Device Directory: {device_dir}")
    print(f"Vendor Directory: {vendor_dir}")

    deleted_files = get_deleted_files_from_git(device_dir, args.commit)
    if not deleted_files:
        print("No deleted files detected in proprietary-files.txt.")
        return

    print(f"\nFound {len(deleted_files)} deleted file(s) from proprietary-files.txt:")
    for f in deleted_files:
        print(f"  - {f}")

    # Generate module names
    module_names = set()
    file_paths = set(deleted_files)
    for fp in deleted_files:
        basename = os.path.basename(fp)
        if basename.endswith('.so'):
            module_names.add(basename[:-3])
        else:
            module_names.add(basename)

    if args.dry_run:
        print("\nDry run completed. No changes made.")
        return

    # 1. Remove physical files
    removed_files = 0
    for fp in deleted_files:
        full_path = vendor_dir / 'proprietary' / fp
        if full_path.exists():
            full_path.unlink()
            removed_files += 1
            # Remove empty parent directories
            parent = full_path.parent
            while parent != vendor_dir / 'proprietary' and parent.exists():
                try:
                    parent.rmdir()
                    parent = parent.parent
                except OSError:
                    break

    print(f"\n[1] Removed {removed_files} physical file(s) from {vendor_dir / 'proprietary'}")

    # 2. Update <device>-vendor.mk
    mk_files = list(vendor_dir.glob('*-vendor.mk'))
    for mk in mk_files:
        copies, pkgs = remove_entries_from_vendor_mk(mk, file_paths, module_names)
        print(f"[2] Updated {mk.name}: removed {copies} COPY rule(s), {pkgs} PACKAGE entry(ies)")

    # 3. Update Android.bp
    bp_path = vendor_dir / 'Android.bp'
    if bp_path.exists():
        bp_removed = remove_block_from_android_bp(bp_path, module_names, file_paths)
        print(f"[3] Updated Android.bp: removed {bp_removed} module block(s)")

    print("\nVendor cleanup completed successfully!")


if __name__ == '__main__':
    main()
