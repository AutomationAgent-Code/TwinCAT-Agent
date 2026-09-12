"""Create a template solution in the explicitly selected, empty XAE."""
import re
from dataclasses import asdict
from pathlib import Path


def template_catalog():
    from .repository import list_templates
    return [asdict(m) for m in list_templates()]


def create_solution(name, output_dir, template):
    from ._ps_bridge import ps_com, tool_target
    from .scaffold import scaffold
    if not re.fullmatch(r'[\w][\w .-]{0,79}', name) or name.endswith(('.', ' ')):
        raise ValueError('项目名必须是单一文件夹名称，不能包含路径分隔符')
    parent = Path(output_dir)
    if not parent.is_absolute():
        raise ValueError('output_dir 必须为绝对路径')
    destination = (parent / name).resolve()
    if destination.parent != parent.resolve() or destination.exists():
        raise ValueError('目标目录已存在或路径无效；不覆盖现有工程')
    info = ps_com('connect-check')
    pid = int(info.get('pid') or 0)
    if not pid or info.get('solution') or info.get('solution_open'):
        raise ValueError('创建解决方案需要已连接且没有打开工程的 XAE')
    with tool_target(pid):
        destination.mkdir(parents=True, exist_ok=False)
        # Scaffold without hooks: opening is performed only in this DTE instance.
        scaffold(template_names=[template], output_dir=str(destination),
                 user_vars={'PROJECT_NAME': name}, interactive=False, no_hooks=True)
        solutions = list(destination.glob('*.sln'))
        if len(solutions) != 1:
            raise ValueError(f'模板必须生成唯一 .sln；生成文件保留在 {destination}')
        result = ps_com('open-created-solution', path=str(solutions[0]), timeout=90)
        actual = ps_com('connect-check')
        if int(actual.get('pid') or 0) != pid or Path(actual.get('solution') or '').resolve() != solutions[0].resolve():
            raise RuntimeError(f'创建后 XAE 回读不一致；生成文件保留在 {destination}')
        return {**result, 'status': 'created', 'verified': True,
                'solution': str(solutions[0]), 'pid': pid, 'template': template}
