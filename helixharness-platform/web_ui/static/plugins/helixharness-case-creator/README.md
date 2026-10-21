# HelixHarness Case Creator

Create portable evaluation Cases from your own application repositories. Requires
Python 3.10+, Git, a plugin-capable Codex installation, and an installed helixharness-platform.

## Install the downloaded ZIP

Extract the ZIP, then run these commands from the extracted `helixharness-case-creator`
directory (the one containing this README):

```bash
codex plugin marketplace add "$PWD"
codex plugin add helixharness-case-creator@helixharness-platform
```

Open a new Codex task in the application repository and invoke `$helixharness-case-creator`.
Tell it the absolute path to your helixharness-platform checkout and evaluation database.
Use the platform virtual environment for Python scripts that import `bench`.
The platform, calibration CLI, and Worker must all use the same database.

Discovery and preflight do not publish or overwrite existing Cases. Follow the
skill's evidence and confirmation steps for publication and calibration. No
company repository, private service, or built-in model credential is provided.

## 从源码安装

也可以在开源项目根目录直接运行：

```bash
codex plugin marketplace add "$PWD/helixharness-platform/web_ui/static/plugins/helixharness-case-creator"
codex plugin add helixharness-case-creator@helixharness-platform
```

安装后新建 Codex 任务，使用 `$helixharness-case-creator`。提供本机平台源码与数据库的
绝对路径，并使用平台虚拟环境运行预检和校准。
