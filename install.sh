#!/bin/bash
DESTINATION="$HOME/.claude/skills/fork-maintainer"
mkdir -p "$DESTINATION"
cp scripts/ references/ SKILL.md README.md "$DESTINATION/"
echo "Skill installed at $DESTINATION"