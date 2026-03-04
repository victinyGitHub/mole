---
name: setup
description: Install the mole CLI tool. Run this first before using mole.
disable-model-invocation: true
---

Install the mole CLI tool:

1. Check if Python 3.10+ is available:
   ```bash
   python3 --version
   ```

2. Check if Claude CLI is available:
   ```bash
   claude --version
   ```

3. Install mole from GitHub:
   ```bash
   pip install "mole-code[pretty] @ git+https://github.com/victinyGitHub/mole.git"
   ```

4. Verify the installation:
   ```bash
   mole --check examples/test_projects/fizzbuzz.py || echo "mole installed but no test file here — try: mole --check on any Python file with hole() calls"
   ```

If pip install fails, try cloning and installing locally:
```bash
git clone https://github.com/victinyGitHub/mole.git /tmp/mole-install
pip install -e "/tmp/mole-install[pretty]"
```

After installation, the `mole` command is available globally. Use `/mole:mole` for usage instructions.
