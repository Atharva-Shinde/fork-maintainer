#!/bin/bash
DESTINATION="$HOME/.claude/skills/fork-maintainer"
mkdir -p "$DESTINATION"
cp fetch.py setup.py patch.py SKILL.md "$DESTINATION/"
echo "Skill installed at $DESTINATION."