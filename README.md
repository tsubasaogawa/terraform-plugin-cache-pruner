# terraform-plugin-cache-pruner

## 概要

Terraform の .terraform.lock.hcl を解析し、~/.terraform.d/plugin_cache にある未使用のプロバイダ／バージョンを検出して一覧化、移動、または削除できる Python スクリプトです。

## 使い方

```sh
# Dry-run
python main.py --repo /path/to/terraform/dir

# バックアップして実際に削除
python main.py \
  --repo /path/to/terraform/dir \
  --backup /tmp/plugin_cache_backup.tgz --execute --remove-empty-root

# バックアップを作成せずに削除
python main.py \
  --repo /path/to/terraform/dir \
  --execute --remove-empty-root
```

## ログ

- デフォルトログ: /tmp/terraform_plugin_cache_prune.log
