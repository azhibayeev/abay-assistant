#!/bin/bash
# Деплой на VPS — запускать из корня проекта
set -e
echo "Pushing to GitHub..."
git push origin main
echo "Deploying on server..."
ssh ubuntu@185.22.67.34 "~/deploy.sh"
echo "Done!"
