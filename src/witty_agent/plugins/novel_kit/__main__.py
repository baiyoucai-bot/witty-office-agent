"""`python -m witty_agent.plugins.novel_kit` 入口。

守卫不能省：`list_tools()` 用 `pkgutil.walk_packages` 扫 `witty_agent.plugins`，
会把本模块当普通子模块导入一遍。没有守卫，注册工具时就会顺手跑一次 CLI 并 SystemExit。
"""

from witty_agent.plugins.novel_kit.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
