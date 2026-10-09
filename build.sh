#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "清理旧构建..."
rm -rf build dist

echo "开始打包..."
pyinstaller UCloudCleaner.spec --noconfirm

# 移除 macOS 隔离属性
xattr -cr dist/UCloudCleaner.app

echo "构建完成：dist/UCloudCleaner.app"
