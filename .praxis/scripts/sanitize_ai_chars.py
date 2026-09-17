import os
from pathlib import Path

def run_sanitization():
    target = "—"
    replacement = " - "
    root_dir = Path(__file__).resolve().parent.parent
    # test commit
    print(f"🚀 Starting recursive sanitization of '{target}' to '{replacement}'...")

    files_processed = 0

    for root, dirs, files in os.walk(root_dir):
        # Prune hidden directories in-place (starts with .)
        dirs[:] = [d for d in dirs if not d.startswith('.')]

        for filename in files:
            # Skip the script itself
            if filename == os.path.basename(__file__):
                continue

            if filename.endswith(".py"):
                continue

            file_path = os.path.join(root, filename)

            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()

                if target in content:
                    new_content = content.replace(target, replacement)
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    print(f"✅ Modified: {file_path}")
                    files_processed += 1

            except (UnicodeDecodeError, PermissionError):
                # Ignore binaries and restricted files
                continue

    print(f"\nDone. {files_processed} files were updated.")

if __name__ == "__main__":
    run_sanitization()