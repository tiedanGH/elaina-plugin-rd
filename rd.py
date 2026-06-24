"""/rd 随机指令插件

安全原则: bot 输出 = (a) 硬编码模板 + (b) 机器随机数 + (c) 占位符。
任何用户输入的字符串内容不进入输出。

特别处理:
  · c / r / rn — 用户元素 [AAA, BBB] → 替换为 [元素1] [元素2] 再抽
  · 智能算式 — 原算式隐藏, 仅输出骰子展开 + 数字结果
            ASCII 白名单杜绝非 ASCII 进入展开式
            ctypes 强制中断超时保护
"""

import re
import math
import random
import asyncio
import ctypes
import threading
from collections import Counter

from core.plugin.decorators import handler
from core.base.logger import get_logger, PLUGIN

log = get_logger(PLUGIN, "rd")


# ==================== 沙箱 (一次性导入) ====================
try:
    from RestrictedPython import compile_restricted, safe_builtins
    from RestrictedPython.Eval import default_guarded_getiter
    from RestrictedPython.Guards import safer_getattr
    _SANDBOX_AVAILABLE = True
except ImportError:
    _SANDBOX_AVAILABLE = False
    compile_restricted = None
    safe_builtins = {}
    default_guarded_getiter = None
    safer_getattr = None
    log.warning("RestrictedPython 未安装，智能算式功能将不可用。安装组件: pip install RestrictedPython")


def _safe_getitem(obj, index):
    try:
        return obj[index]
    except (IndexError, KeyError, TypeError):
        raise ValueError('unsafe item access')


if _SANDBOX_AVAILABLE:
    _POLICY_GLOBALS = {
        '__builtins__': safe_builtins,
        '_getiter_': default_guarded_getiter,
        '_getattr_': safer_getattr,
        '_getitem_': _safe_getitem,
    }
    _POLICY_GLOBALS.update({n: getattr(math, n) for n in dir(math) if not n.startswith('_')})
    _POLICY_GLOBALS.update({n: getattr(random, n) for n in dir(random) if not n.startswith('_')})
else:
    _POLICY_GLOBALS = {}


_DICE_TIMEOUT_S = 1.5
_MAX_RESULT_LEN = 300
_NUM_RANGE_LIMIT = 10 ** 20


# ==================== 帮助文案 (纯文本) ====================

_INFO = (
    "本插件使用沙箱环境运行随机/算式计算\n"
    "目前已实现的功能有：\n"
    "**XdY、c、r、rn、i、ic、ir、f**\n"
    "> 具体使用方法请查看帮助：/rd help"
)

_HELP = (
    "```指令帮助\n"
    "/rd                       1~100 随机整数\n"
    "/rd <智能算式(≥3 字符)>    支持执行算式、数学函数的调用（仅 ASCII）\n"
    "                          例: d20+3 / 3d6*2 / sin(pi/2)\n"
    "/rd c <n> <元素列表>      抽取 n 个不重复的元素\n"
    "/rd r <n> <元素列表>      抽取 n 个可重复的元素\n"
    "/rd rn <n> <元素列表>     抽取 n 个可重复的元素（合并计数）\n"
    "/rd i [下界] <上界>       随机 1 个整数\n"
    "/rd ic <n> [下界] <上界>  随机 n 个不重复的整数\n"
    "/rd ir <n> [下界] <上界>  随机 n 个可重复的整数\n"
    "/rd f <n> <下界> <上界> [小数位]   随机 n 个浮点数, 默认 3 位\n"
    "```\n"
    "> 数值范围 ±10^20\n"
    "> n 上限 20 (rn 1000000)"
)


# ==================== 强制中断超时 ====================

def _run_with_kill_timeout(fn, args, timeout):
    """同步运行 fn(*args), 超时用 ctypes 强制注入 TimeoutError 终止线程.

    跨平台 (Linux/Windows 一致行为). 即使被 fn 内部 catch 了 TimeoutError,
    ThreadKiller 风格的反复注入也能确保最终退出 (这里简化为一次注入 + 短 join).

    返回 fn 的结果元组, 或超时时返回固定的 timeout 错误元组。
    """
    container = {'value': None}

    def runner():
        try:
            container['value'] = fn(*args)
        except TimeoutError:
            container['value'] = (None, None, f"[运行超时] 算式计算超过时间限制 {timeout:.1f}s")
        except Exception:
            container['value'] = (None, None, "[执行失败] 发生未知错误")

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout)

    if t.is_alive():
        # 注入 TimeoutError 强制中断 (与老项目 ThreadKiller 思路一致)
        tid = t.ident
        if tid is not None:
            try:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_long(tid),
                    ctypes.py_object(TimeoutError),
                )
            except Exception:
                pass
            t.join(0.3)  # 给线程一点时间响应注入
        # 无论是否成功 kill, 主流程立即返回超时 (后台 daemon 线程退不掉也不影响)
        return None, None, f"[运行超时] 算式计算超过时间限制 {timeout:.1f}s"

    return container['value']


# ==================== 工具函数 ====================

def _to_int(s):
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def _to_float(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _check_range(start, end, n):
    if not (1 <= n <= 20):
        return "[错误] 个数 n 必须为 1~20"
    if start >= end:
        return "[错误] 上界必须大于下界"
    if start < -_NUM_RANGE_LIMIT:
        return "[错误] 下界超过最小限制 (-10^20)"
    if end > _NUM_RANGE_LIMIT:
        return "[错误] 上界超过最大限制 (10^20)"
    return None


# ==================== 智能算式 ====================
# ASCII 白名单: 0x20-0x7E 之间的可打印 ASCII; 排除中文等非 ASCII 字符
_ASCII_ONLY = re.compile(r'^[\x20-\x7E]+$')


def _dice_compute(expression):
    """同步: 展开骰子 + 沙箱求值. 返回 (final_expr, result_str, error).

    注意: 本函数在 _run_with_kill_timeout 启动的子线程内执行;
    若调用方主线程 join 超时, 会通过 ctypes 注入 TimeoutError 中断本函数。
    """
    if not _SANDBOX_AVAILABLE:
        return None, None, "[执行失败] 运行环境错误：RestrictedPython 沙箱组件未安装"
    if not expression or len(expression) < 3:
        return None, None, "[错误] 算式过短 (至少 3 字符)"
    if not _ASCII_ONLY.match(expression):
        return None, None, "[禁止执行] 算式仅允许白名单 ASCII 字符"

    # 拆分并展开 XdY
    with_d = False  # 跟踪是否含真实骰子 (老脚本同名变量)
    try:
        segments = re.split(r'([\+\-\*\/\(\)\,\.\=\[\]])', expression)
        result_parts = []
        eval_parts = []
        for seg in segments:
            if seg in ['+', '-', '*', '/', '^', '(', ')', ',', '.', '=', '[', ']']:
                result_parts.append(seg)
                eval_parts.append(seg)
            elif 'd' in seg or 'D' in seg:
                try:
                    parts = re.split(r'd|D', seg)
                    x = 1 if parts[0] == '' else int(parts[0])
                    y = int(parts[1])
                    if not (1 <= x <= 1000000 and 1 <= y <= 1000000):
                        return None, None, "[错误] 骰子参数越界 (x, y 1~1000000)"
                    if len(parts) > 2:
                        return None, None, "[错误] 骰子格式错误 (d 不能连用)"
                    rolls = [random.randint(1, y) for _ in range(x)]
                    result_parts.append(f"({'+'.join(map(str, rolls))})")
                    eval_parts.append(str(sum(rolls)))
                    with_d = True
                except (ValueError, TypeError):
                    result_parts.append(seg)
                    eval_parts.append(seg)
            else:
                result_parts.append(seg)
                eval_parts.append(seg)
        final_expr = ''.join(result_parts)
        eval_expression = ''.join(eval_parts)
    except Exception:
        return None, None, "[错误] 算式解析失败"

    eval_expression = re.sub(r'print', '', eval_expression)

    # 沙箱求值
    try:
        compiled = compile_restricted(eval_expression, filename='<rd>', mode='eval')
        result = eval(compiled, _POLICY_GLOBALS, {})
    except SyntaxError:
        return None, None, "[语法错误] 算式存在无法识别的字符或括号缺失"
    except NameError:
        return None, None, "[语法错误] 算式含未定义的标识符"
    except ZeroDivisionError:
        return None, None, "[数学错误] 除数不能为 0"
    except OverflowError:
        return None, None, "[数学错误] 数值溢出"
    except (ValueError, ArithmeticError):
        return None, None, "[数学错误] 算式不符合定义域"
    except PermissionError:
        return None, None, "[禁止执行] 表达式含被限制的内容"
    except TimeoutError:
        # ctypes 注入的中断, 上抛让 _run_with_kill_timeout 转成统一超时模板
        raise
    except Exception:
        return None, None, "[执行失败] 算式无法求值"

    # 结果必须是数字类型 (杜绝字符串/列表等"内容"输出)
    if isinstance(result, bool):
        result = int(result)
    if not isinstance(result, (int, float)):
        return None, None, "[数学错误] 算式结果不是数字"

    # ----- 第一层截断: result 太长 (>300) ----- (照搬老脚本)
    result_str = str(result)
    raw_len = len(result_str)
    if raw_len > _MAX_RESULT_LEN:
        if isinstance(result, int):
            try:
                head = result_str[:11]
                result_str = f"{int(head) / 10**10}e+{raw_len - 1}"
            except Exception:
                result_str = "[结果过长]"
        else:
            result_str = (
                result_str[:_MAX_RESULT_LEN]
                + f"......\n[剩余{raw_len - _MAX_RESULT_LEN}字符被省略]"
            )

    # ----- 第二层截断: 整体过长 (>500) → 用 eval_expression 替代 final_expr -----
    # 100d6 这种会让 final_expr 爆长成 (1+3+5+...) 200+ 字符;
    # 用 eval_expression (已求和的简化式) 替代避免整条消息超 QQ 上限
    if not with_d:
        # 无骰子: 不显示展开 (避免直接回显用户原算式)
        display_expr = ''
    elif len(f"{final_expr}{result_str}") <= 500:
        display_expr = final_expr
    else:
        display_expr = eval_expression

    return display_expr, result_str, None


async def _run_dice(expression):
    """异步入口: 委托到工作线程; 内部线程负责强制中断"""
    try:
        return await asyncio.to_thread(
            _run_with_kill_timeout, _dice_compute, (expression,), _DICE_TIMEOUT_S,
        )
    except Exception:
        return None, None, "[执行失败] 发生未知错误"


# ==================== 子命令: 数字类 (老项目纯文本格式) ====================

def _cmd_random_1_100():
    return f"> **执行指令：**\n> 1~100中随机整数\n\n**随机结果：**\n{random.randint(1, 100)}"


def _cmd_i(args):
    if len(args) < 1:
        return "[参数不足]\n/rd i [下界] <上界>   随机生成 1 个整数"
    if len(args) == 1:
        start, end = 1, _to_int(args[0])
    else:
        start, end = _to_int(args[0]), _to_int(args[1])
    if start is None or end is None:
        return "[错误] 上下界必须为整数"
    err = _check_range(start, end, 1)
    if err:
        return err
    return f"> **执行指令：**\n> 范围内随机整数\n\n**随机结果：**\n{random.randint(start, end)}"


def _cmd_ic(args):
    if len(args) < 2:
        return "[参数不足]\n/rd ic <n> [下界] <上界>   随机生成 n 个不重复的整数"
    n = _to_int(args[0])
    if n is None:
        return "[错误] n 必须为整数"
    if len(args) == 2:
        start, end = 1, _to_int(args[1])
    else:
        start, end = _to_int(args[1]), _to_int(args[2])
    if start is None or end is None:
        return "[错误] 上下界必须为整数"
    err = _check_range(start, end, n)
    if err:
        return err
    if n > end - start + 1:
        return "[错误] 范围内整数数量不足"
    pool = list(range(start, end + 1))
    random.shuffle(pool)
    body = '\n'.join(str(x) for x in pool[:n])
    return f"> **执行指令：**\n> 范围内随机{n}个不重复整数\n\n**随机结果：**\n{body}"


def _cmd_ir(args):
    if len(args) < 2:
        return "[参数不足]\n/rd ir <n> [下界] <上界>   随机生成 n 个可重复的整数"
    n = _to_int(args[0])
    if n is None:
        return "[错误] n 必须为整数"
    if len(args) == 2:
        start, end = 1, _to_int(args[1])
    else:
        start, end = _to_int(args[1]), _to_int(args[2])
    if start is None or end is None:
        return "[错误] 上下界必须为整数"
    err = _check_range(start, end, n)
    if err:
        return err
    body = '\n'.join(str(random.randint(start, end)) for _ in range(n))
    return f"> **执行指令：**\n> 范围内随机{n}个可重复整数\n\n**随机结果：**\n{body}"


def _cmd_f(args):
    if len(args) < 3:
        return "[参数不足]\n/rd f <n> <下界> <上界> [小数位]   随机生成 n 个范围内的浮点数"
    n = _to_int(args[0])
    start = _to_float(args[1])
    end = _to_float(args[2])
    decimal = 3 if len(args) == 3 else _to_int(args[3])
    if n is None or decimal is None:
        return "[错误] n 与小数位必须为整数"
    if start is None or end is None:
        return "[错误] 上下界必须为数字"
    if not (1 <= decimal <= 20):
        return "[错误] 小数位必须为 1~20"
    err = _check_range(start, end, n)
    if err:
        return err
    try:
        lo_int = int(start * 10 ** decimal)
        hi_int = int(end * 10 ** decimal)
        if hi_int <= lo_int:
            return "[错误] 上下界差距过小"
        vals = [random.randint(lo_int, hi_int) / 10 ** decimal for _ in range(n)]
    except Exception:
        return "[执行失败] 浮点范围计算异常"
    body = '\n'.join(f"{v:.{decimal}f}" for v in vals)
    return f"> **执行指令：**\n> 范围内随机{n}个{decimal}位浮点数\n\n**随机结果：**\n{body}"


# ==================== 子命令: 元素类 (用占位符 [元素N] 替换) ====================

def _mask_elements(raw_elements):
    """把用户元素 [AAA, BBB, CCC] 替换为占位符 [[元素1], [元素2], [元素3]]

    不考虑用户重复 — 即使 AAA AAA AAA 三个相同, 也视为三个不同元素。
    安全保证: 占位符是硬编码 + 数字, 不含任何用户输入。
    """
    return [f"[元素{i + 1}]" for i in range(len(raw_elements))]


def _cmd_c(args):
    if len(args) < 2:
        return "[参数不足]\n/rd c <n> <元素列表>   抽取 n 个不重复的元素"
    n = _to_int(args[0])
    if n is None:
        return "[错误] n 必须为整数"
    if not (1 <= n <= 20):
        return "[错误] n 必须为 1~20"
    raw_elements = args[1:]
    if not raw_elements:
        return "[错误] 至少需要 1 个元素"
    masked = _mask_elements(raw_elements)
    if n > len(masked):
        return "[错误] 抽取个数超过元素总数"
    random.shuffle(masked)
    body = '\n'.join(masked[:n])
    return f"> **执行指令：**\n> 抽取{n}个不重复的元素\n\n**随机结果：**\n{body}"


def _cmd_r(args):
    if len(args) < 2:
        return "[参数不足]\n/rd r <n> <元素列表>   抽取 n 个可重复的元素"
    n = _to_int(args[0])
    if n is None:
        return "[错误] n 必须为整数"
    if not (1 <= n <= 20):
        return "[错误] n 必须为 1~20"
    raw_elements = args[1:]
    if not raw_elements:
        return "[错误] 至少需要 1 个元素"
    masked = _mask_elements(raw_elements)
    body = '\n'.join(random.choice(masked) for _ in range(n))
    return f"> **执行指令：**\n> 抽取{n}个可重复的元素\n\n**随机结果：**\n{body}"


def _cmd_rn(args):
    if len(args) < 2:
        return "[参数不足]\n/rd rn <n> <元素列表>   抽取 n 个可重复的元素 (合并计数)"
    n = _to_int(args[0])
    if n is None:
        return "[错误] n 必须为整数"
    if n < 1:
        return "[错误] n 必须为正数"
    if n > 10 ** 6:
        return "[错误] n 不能超过 1000000"
    raw_elements = args[1:]
    if not raw_elements:
        return "[错误] 至少需要 1 个元素"
    if len(raw_elements) > 20:
        return "[错误] rn 的元素种类不能超过 20 个"
    masked = _mask_elements(raw_elements)
    counter = Counter(random.choice(masked) for _ in range(n))
    body = '\n'.join(f"{k} ×{v}" for k, v in sorted(counter.items()))
    return f"> **执行指令：**\n> 抽取{n}个可重复的元素\n\n**随机结果：**\n{body}"


# ==================== 主入口 ====================

@handler(r'^[/#]rd(?:\s+(.+?))?\s*$',
         name='随机工具',
         desc='随机骰子/智能算式 (/rd help 查看帮助)',)
async def cmd_rd(event, match):
    raw = match.group(1)
    args = raw.split() if raw else []

    if not args:
        result = _cmd_random_1_100()
    else:
        sub = args[0]
        if sub in ('info', '信息'):
            result = _INFO
        elif sub in ('help', '帮助'):
            result = _HELP
        elif sub == 'c':
            result = _cmd_c(args[1:])
        elif sub == 'r':
            result = _cmd_r(args[1:])
        elif sub == 'rn':
            result = _cmd_rn(args[1:])
        elif sub == 'i':
            result = _cmd_i(args[1:])
        elif sub == 'ic':
            result = _cmd_ic(args[1:])
        elif sub == 'ir':
            result = _cmd_ir(args[1:])
        elif sub == 'f':
            result = _cmd_f(args[1:])
        else:
            # 智能算式 — 整段 raw 作为表达式 (不回显 sub)
            full_expr = raw.strip() if raw else ''
            if len(full_expr) < 3:
                result = "[不支持的指令]\n请使用「/rd help」查看指令帮助"
            else:
                final_expr, num, err = await _run_dice(full_expr)
                if num is not None:
                    # final_expr 为空 = 无骰子 (老脚本 with_d=False 时不显示展开)
                    # 或第二层截断已用 eval_expression 替代过长的骰子展开
                    body = f"{final_expr}={num}" if final_expr else f"{num}"
                    result = (
                        "> **执行指令：**\n"
                        "> ···[算式已隐藏]···\n\n"
                        "**运算结果：**\n"
                        f"{body}"
                    )
                else:
                    result = err or "[执行失败]"

    # 引用回复触发消息: 通过 kwargs 透传 message_reference 字段
    # (sender._build_core_payload 末尾 payload.update(kwargs) 会把它合并进 payload)
    kwargs = {}
    if event.message_id:
        kwargs['message_reference'] = {
            'message_id': event.message_id,
            'ignore_get_message_error': True,
        }
    await event.reply(result, **kwargs)
