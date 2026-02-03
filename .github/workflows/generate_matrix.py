import subprocess
import json
import os
import sys
import logging
import argparse

def setup_logger(level):
    """Set up the logger with specified logging level."""
    logger = logging.getLogger('generate_matrix')
    logger.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)

    logger.addHandler(handler)
    return logger

def get_changed_files(start_ref, end_ref, logger):
    """Get a list of changed files between two Git references."""
    cmd = ["git", "diff", "--name-only", start_ref, end_ref]
    logger.info(f"Running command: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        logger.error(f"Git command failed with exit code {result.returncode}: {result.stderr}")
        sys.exit(1)

    logger.debug(f"Command output: {result.stdout}")
    return result.stdout.strip().split('\n')

def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Generate a matrix for GitHub Actions based on changed files.")
    parser.add_argument("-s", "--start-ref", required=True, help="The start Git reference.")
    parser.add_argument("-e", "--end-ref", required=True, help="The end Git reference.")
    parser.add_argument("-o", "--output-path", required=True, help="The output file path for the matrix JSON.")
    parser.add_argument("-l", "--log-level", default="INFO", choices=["DEBUG", "INFO"], help="Set the logging level (default: INFO).")
    return parser.parse_args()

def main():
    """Main function to generate a matrix based on directories containing any changed TARGET_FILES."""
    TARGET_FILES = ["execution-environment.yml", "requirements.txt", "requirements.yml", "bindep.txt", "customize.sh"]
    args = parse_arguments()
    log_level = logging.DEBUG if args.log_level == 'DEBUG' else logging.INFO
    logger = setup_logger(log_level)

    logger.info(f"Start ref: {args.start_ref}, End ref: {args.end_ref}")

    # determine if end_ref is a commit hash or a branch name
    if len(args.end_ref) == 40 and all(c in '0123456789abcdef' for c in args.end_ref.lower()):
        # commit hash
        end_ref = args.end_ref
    else:
        # branch name
        end_ref = "refs/remotes/origin/" + args.end_ref

    changed_files = get_changed_files(args.start_ref, end_ref, logger)
    dirs = set()

    for file in changed_files:
        current_dir = os.path.dirname(file)
        workspace = os.getenv('GITHUB_WORKSPACE', '')

        # Walk up the directory tree
        while current_dir != "":
            # Check for any of our trigger files in the current level
            if any(os.path.isfile(os.path.join(workspace, current_dir, t)) for t in TARGET_FILES):
                if current_dir not in dirs:
                    logger.info(f"Found build context at: {current_dir}")
                    # only need the first directory no matter how deep in the path the modified file is found
                    dirs.add(current_dir.split(os.sep)[0])
                break # Found the context for this file, stop climbing
            
            # Move to parent
            parent = os.path.dirname(current_dir)
            if parent == current_dir: break
            current_dir = parent

    matrix = {'include': [{'ee': dir_name} for dir_name in dirs]}
    logger.info(f"Generated matrix: {json.dumps(matrix, indent=4)}")

    with open(args.output_path, 'w') as file:
        file.write(json.dumps(matrix))

if __name__ == "__main__":
    main()