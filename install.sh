#!/bin/bash
DESTINATION="$HOME/.claude/skills/fork-maintainer"
mkdir -p "$DESTINATION"
cp fetch.py setup.py branch_setup.py patch.py tag_creation.py pullrequest.py SKILL.md README.md "$DESTINATION/"
echo "Skill installed at $DESTINATION"