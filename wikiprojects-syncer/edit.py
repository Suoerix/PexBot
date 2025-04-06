# -*- coding: utf-8 -*-
import pywikibot
import json
import re
import time
import os
import mwparserfromhell  # 使用 mwparserfromhell 处理模板更稳健
from pywikibot import textlib
from pywikibot.exceptions import (
    NoPageError, IsRedirectPageError, APIError, InvalidTitleError,
    UnknownSiteError, LockedPageError, OtherPageSaveError
)
from pywikibot.data import api

# --- 配置 ---
json_file_path = '1.json'  # 输入的 JSON 文件路径
CACHE_FILE = 'template_mapping_cache.json' # 模板映射缓存文件
edit_summary = '[[WP:机器人/申请/PexBot|从英维同步专题模板]]：' # 编辑摘要
dry_run = False  # 设置为 True 进行测试运行，不实际保存页面
use_bot_flag = True # 编辑时使用机器人标记

# --- 英文维基百科排除列表（小写） ---
excluded_en_projects_lower = {
    'wikiproject articles for creation',
    'wikiproject spoken wikipedia'
}

# --- WPBS 模板名称（小写，用于识别） ---
# 英文 WPBS 名称
en_wpbs_names_lower = {'wikiproject banner shell','wpbs', 'wikiprojectbanners', 'wikiproject banners', 'wpb', 'wikiproject cooperation shell', 'wikiprojectbannershell', 'wpbannershell'}
# 中文 WPBS 名称 (包括可能的重定向)
zh_wpbs_names_lower = {'wikiproject banner shell', 'wpbs', 'wpbannershell', 'wikiprojectbanners', 'multiple wikiprojects', 'wikiprojectbannershell', '多个专题', 'wikiproject shell'}

# 默认使用的中文 WPBS 模板名
default_zh_wpbs_name = 'WikiProject banner shell'

# --- 全局变量 ---
template_map_cache = {} # 英文模板 -> 中文模板 映射缓存 (从文件加载)
zh_template_redirect_cache = {} # 中文模板重定向缓存 (内存中)
site_objects = {} # 存储站点对象
processed_counter = 0
edits_made = 0
skipped_no_zh_page = 0
skipped_no_en_talk = 0
skipped_en_talk_redirect = 0
skipped_zh_talk_redirect = 0
skipped_no_relevant_en_banners = 0
skipped_no_mapping = 0
skipped_no_new_banners_or_importance_updates = 0 # 重命名计数器
skipped_creation_no_banners = 0
error_en_talk_fetch = 0
error_zh_talk_fetch = 0
error_wd_fetch = 0
error_map_fetch = 0
error_zh_save = 0
error_other = 0

# --- 缓存函数 ---
def load_cache(filename):
    """从 JSON 文件加载缓存"""
    if os.path.exists(filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
                pywikibot.output(f"成功从 {filename} 加载了 {len(cache_data)} 条缓存记录。")
                return cache_data
        except (json.JSONDecodeError, IOError, TypeError) as e:
            pywikibot.error(f"读取或解析缓存文件 {filename} 失败: {e}。将使用空缓存。")
    else:
        pywikibot.output(f"缓存文件 {filename} 不存在，将创建新缓存。")
    return {}

def save_cache(cache, filename):
    """将缓存保存到 JSON 文件"""
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        pywikibot.output(f"模板映射缓存已成功保存到 {filename} ({len(cache)} 条记录)。")
    except IOError as e:
        pywikibot.error(f"无法写入缓存文件 {filename}: {e}")
    except Exception as e:
        pywikibot.error(f"保存缓存时发生未知错误: {e}")

# --- 初始化站点 ---
def initialize_sites():
    """初始化并检查维基站点对象"""
    global site_objects
    sites = {'en': 'wikipedia', 'zh': 'wikipedia', 'wikidata': 'wikidata'}
    all_ok = True
    for code, family in sites.items():
        try:
            site = pywikibot.Site(code, family)
            site.login() # 确保登录
            user = site.user()
            if not user:
                 pywikibot.warning(f"未能确认在 {code}.{family} 上的登录用户。请检查认证配置。")
                 # all_ok = False # 允许继续，但发出警告
            else:
                 pywikibot.output(f"成功连接到 {code}.{family} 并确认为用户: {user}")
            site_objects[code] = site
        except UnknownSiteError as e:
            pywikibot.error(f"无法识别站点: {e}。脚本将退出。")
            return False
        except APIError as e:
            pywikibot.error(f"初始化站点 {code}.{family} 或检查登录时发生 API 错误 (可能是认证问题): {e}")
            pywikibot.error("请确保环境已正确配置认证。脚本将退出。")
            return False
        except Exception as e:
            pywikibot.error(f"初始化站点 {code}.{family} 时发生未知错误: {e}")
            return False
    # 检查 Wikidata 是否可用
    if 'wikidata' not in site_objects or not site_objects['wikidata']:
         pywikibot.error("Wikidata 站点未能成功初始化，脚本无法继续。")
         return False

    return True

# --- Wikidata 相关函数 ---
def get_itempage_from_page(page: pywikibot.Page) -> pywikibot.ItemPage | None:
    """获取页面对应的 Wikidata ItemPage"""
    try:
        if not page.exists() or page.namespace() < 0 : # 检查存在性和命名空间
             pywikibot.output(f"...页面 '{page.title()}' 不存在或无效 (ns={page.namespace()})。")
             return None
        if page.isRedirectPage():
             target_page = page.getRedirectTarget()
             pywikibot.output(f"...页面 '{page.title()}' 重定向到 '{target_page.title()}'，尝试获取目标页的 Item。")
             page = target_page # 使用重定向目标页
             # 再次检查目标页是否存在
             if not page.exists() or page.namespace() < 0:
                  pywikibot.output(f"...重定向目标页面 '{page.title()}' 不存在或无效 (ns={page.namespace()})。")
                  return None

        # 使用 data_item() 获取 Wikidata 条目
        item = pywikibot.ItemPage.fromPage(page, lazy_load=False) # 直接尝试获取，内部处理 NoPage

        if item and item.exists():
            return item
        else:
            # fromPage 没找到会抛 NoPageError 或返回不存在的 ItemPage
            pywikibot.output(f"...页面 '{page.title()}' 没有找到对应的 Wikidata 条目。")
            return None
    except NoPageError:
        pywikibot.output(f"...处理页面 '{page.title()}' 时未找到对应的 Wikidata 条目 (NoPageError)。")
        return None
    except APIError as e:
        global error_wd_fetch
        pywikibot.error(f"...获取页面 '{page.title()}' 的 Wikidata 条目时发生 API 错误: {e}")
        error_wd_fetch += 1
        return None
    except Exception as e:
        global error_other
        pywikibot.error(f"...获取页面 '{page.title()}' 的 Wikidata 条目时发生未知错误: {e}")
        error_other += 1
        import traceback; traceback.print_exc()
        return None

def get_zh_page_from_en_title(en_title: str) -> pywikibot.Page | None:
    """通过 Wikidata 获取英文标题对应的中文维基页面对象，处理重定向"""
    global skipped_no_zh_page, error_wd_fetch
    en_page = pywikibot.Page(site_objects['en'], en_title)
    # 不再在这里检查英文页面是否存在或重定向，让 get_itempage_from_page 处理

    item = get_itempage_from_page(en_page)
    if not item:
        pywikibot.output(f"未能获取英文页面 '{en_title}' 的 Wikidata 条目，无法查找中文链接。")
        skipped_no_zh_page += 1
        return None

    try:
        # get(get_redirect=True) 对 sitelinks 可能不适用，直接获取
        sitelinks = item.get()['sitelinks']
        if 'zhwiki' in sitelinks:
            zh_title = sitelinks['zhwiki'].title
            zh_page = pywikibot.Page(site_objects['zh'], zh_title)
            pywikibot.output(f"通过 Wikidata 找到对应中文页面: '{zh_page.title()}'")

            # 检查中文页面是否存在以及是否是重定向
            if not zh_page.exists():
                pywikibot.warning(f"Wikidata 指向的中文页面 '{zh_title}' 不存在，跳过。")
                skipped_no_zh_page += 1
                return None
            if zh_page.isRedirectPage():
                try:
                    target_zh_page = zh_page.getRedirectTarget()
                    # 检查重定向目标是否存在
                    if not target_zh_page.exists():
                         pywikibot.warning(f"中文页面 '{zh_page.title()}' 重定向到的目标 '{target_zh_page.title()}' 不存在，跳过。")
                         skipped_no_zh_page += 1
                         return None
                    pywikibot.output(f"...中文页面重定向到: '{target_zh_page.title()}'，使用目标页面。")
                    return target_zh_page
                except pywikibot.exceptions.CircularRedirectError:
                     pywikibot.error(f"处理中文页面 '{zh_page.title()}' 时检测到循环重定向，跳过。")
                     skipped_no_zh_page += 1
                     return None
                except Exception as e:
                    pywikibot.error(f"获取中文页面 '{zh_page.title()}' 的重定向目标时出错: {e}，跳过。")
                    skipped_no_zh_page += 1
                    return None
            else:
                return zh_page # 非重定向，直接返回
        else:
            pywikibot.output(f"Wikidata 条目 {item.title()} 中没有 'zhwiki' 链接，跳过 '{en_title}'。")
            skipped_no_zh_page += 1
            return None
    except APIError as e:
        pywikibot.error(f"...获取 Wikidata 条目 {item.title()} 的 sitelinks 时发生 API 错误: {e}")
        error_wd_fetch += 1
        skipped_no_zh_page += 1
        return None
    except Exception as e:
        global error_other
        pywikibot.error(f"...获取 Wikidata 条目 {item.title()} 的 sitelinks 时发生未知错误: {e}")
        error_other += 1
        skipped_no_zh_page += 1
        import traceback; traceback.print_exc()
        return None

def get_zh_template_name_from_en(en_template_name: str) -> str | None:
    """
    查找英文模板对应的中文模板名称。
    优先使用全局缓存 `template_map_cache`。
    如果缓存未命中，则通过 Wikidata 查询，并将结果存入缓存。
    返回中文模板名（不带 "Template:" 前缀），如果找不到则返回 None。
    """
    global template_map_cache, error_map_fetch, error_wd_fetch

    # 规范化英文模板名（移除前缀，替换下划线）
    clean_en_name = en_template_name.strip().replace('_', ' ')
    if clean_en_name.lower().startswith('template:'):
        query_name = clean_en_name[len('template:'):].strip()
    else:
        query_name = clean_en_name
    if not query_name: return None

    # 检查缓存
    if query_name in template_map_cache:
        cached_result = template_map_cache[query_name]
        return cached_result # 返回缓存结果，可能是 None

    pywikibot.output(f"开始查找映射: 英文模板 '{query_name}' -> 中文模板?")
    zh_template_found_name = None
    try:
        # 尝试找到英文模板页面 (处理 Template: 前缀和大小写)
        en_template_page = pywikibot.Page(site_objects['en'], f"Template:{query_name}")

        # 获取对应的 Wikidata Item (get_itempage_from_page 会处理不存在和重定向)
        item = get_itempage_from_page(en_template_page)
        if not item:
             # 如果 Template:xxx 找不到 Item，尝试直接用 xxx 找 (可能是直接页面名)
             maybe_page = pywikibot.Page(site_objects['en'], query_name)
             if maybe_page.namespace() == 10: # 确保是模板命名空间
                  item = get_itempage_from_page(maybe_page)

        if not item: # 如果两种方式都找不到 Item
             pywikibot.output(f"...英文模板 '{query_name}' 未找到有效的页面或对应的 Wikidata 条目。")
             template_map_cache[query_name] = None
             return None

        # 从 Wikidata 获取中文链接
        sitelinks = item.get()['sitelinks'] # get_redirect=True 不适用于sitelinks
        if 'zhwiki' in sitelinks:
            zh_link_title = sitelinks['zhwiki'].title
            # 提取模板名（移除 Template: 前缀）
            if zh_link_title.lower().startswith('template:'):
                zh_template_found_name = zh_link_title[len('template:'):].strip()
            else:
                # 检查链接是否在模板命名空间
                zh_link_page = pywikibot.Page(site_objects['zh'], zh_link_title)
                if zh_link_page.namespace() == 10:
                     zh_template_found_name = zh_link_title.strip() # 如果在模板命名空间，即使没前缀也用
                else:
                     pywikibot.warning(f"...Wikidata 找到的中文链接 '{zh_link_title}' 不在 Template 命名空间 (ns={zh_link_page.namespace()})，忽略此映射。")
                     zh_template_found_name = None

            if zh_template_found_name:
                 pywikibot.output(f"...通过 Wikidata 找到中文模板: '{zh_template_found_name}'")
            # else: (如果解析后为空或命名空间不对) zh_template_found_name 保持 None

        else:
            pywikibot.output(f"...Wikidata 条目 {item.title()} 没有中文维基 ('zhwiki') sitelink。")
            zh_template_found_name = None

    except InvalidTitleError as e:
        pywikibot.error(f"处理英文模板名 '{query_name}' 时标题无效: {e}")
        error_map_fetch += 1
        zh_template_found_name = None
    except APIError as e:
        pywikibot.error(f"查找英文模板 '{query_name}' 的映射时发生 API 错误: {e}")
        error_map_fetch += 1
        if "ratelimited" in str(e).lower(): time.sleep(10)
        zh_template_found_name = None
    except Exception as e:
        global error_other
        pywikibot.error(f"查找英文模板 '{query_name}' 的映射时发生未知错误: {e}")
        error_other += 1
        import traceback; traceback.print_exc()
        zh_template_found_name = None

    # 更新缓存
    template_map_cache[query_name] = zh_template_found_name
    return zh_template_found_name

# --- 中文模板处理函数 ---
def get_canonical_zh_template_name(zh_template_name: str) -> str | None:
    """
    获取中文模板的规范名称（解析重定向），使用内存缓存 `zh_template_redirect_cache`。
    返回规范化的模板名（不带 "Template:" 前缀），如果模板不存在或无效则返回 None。
    """
    global zh_template_redirect_cache, error_other

    # 规范化输入名
    clean_zh_name = zh_template_name.strip().replace('_', ' ')
    if not clean_zh_name: return None

    # 检查内存缓存
    if clean_zh_name in zh_template_redirect_cache:
        return zh_template_redirect_cache[clean_zh_name]

    canonical_name = None
    try:
        # 优先检查带 Template: 前缀的页面
        zh_template_page = pywikibot.Page(site_objects['zh'], f"Template:{clean_zh_name}")
        page_to_check = zh_template_page

        if not zh_template_page.exists():
             # 如果带前缀的不存在，尝试不带前缀的（可能直接引用了名字）
             maybe_page = pywikibot.Page(site_objects['zh'], clean_zh_name)
             if maybe_page.exists() and maybe_page.namespace() == 10:
                 page_to_check = maybe_page
                 # pywikibot.output(f"...检查规范名：'{clean_zh_name}' 作为页面名存在且是模板。")
             else:
                  # 如果两种方式都找不到，或者找到的不是模板，则认为模板不存在
                  if maybe_page.exists() and maybe_page.namespace() != 10:
                       # pywikibot.warning(f"...检查规范名：页面 '{clean_zh_name}' 存在但不是模板 (ns={maybe_page.namespace()})。")
                       pass # 不是模板，当做无效
                  # else:
                       # pywikibot.warning(f"...检查规范名：中文模板 'Template:{clean_zh_name}' 或 '{clean_zh_name}' (ns=10) 不存在。")
                  zh_template_redirect_cache[clean_zh_name] = None
                  return None

        # 检查页面是否是重定向
        if page_to_check.isRedirectPage():
            target_page = page_to_check.getRedirectTarget()
            # 检查重定向目标是否还是模板
            if target_page.namespace() == 10:
                 target_title = target_page.title(with_ns=False).strip().replace('_', ' ')
                 if target_title:
                     # pywikibot.output(f"...中文模板 '{clean_zh_name}' (检查的是 '{page_to_check.title()}') 重定向到 -> '{target_title}'")
                     canonical_name = target_title
                 else: canonical_name = None # 目标名为空
            else:
                 pywikibot.warning(f"...中文模板 '{clean_zh_name}' 重定向目标 '{target_page.title()}' 不在模板命名空间 (ns={target_page.namespace()})，视为无效。")
                 canonical_name = None
        else: # 不是重定向
            # 确保页面本身在模板命名空间
            if page_to_check.namespace() == 10:
                 base_title = page_to_check.title(with_ns=False).strip().replace('_', ' ')
                 if base_title: canonical_name = base_title
                 else: canonical_name = None # 名字为空？
            else: # 页面不在模板命名空间
                 # pywikibot.warning(f"...页面 '{page_to_check.title()}' 不是模板命名空间 (ns={page_to_check.namespace()})，视为无效。")
                 canonical_name = None

    except InvalidTitleError as e:
        pywikibot.error(f"检查中文模板规范名时标题无效 '{clean_zh_name}': {e}")
        canonical_name = None
    except APIError as e:
        pywikibot.error(f"检查中文模板 '{clean_zh_name}' 时发生 API 错误: {e}")
        return None # 不将 None 存入缓存，下次可以重试
    except Exception as e:
        pywikibot.error(f"检查中文模板 '{clean_zh_name}' 时发生未知错误: {e}")
        error_other += 1
        import traceback; traceback.print_exc()
        canonical_name = None # 未知错误，不确定规范名

    # 更新缓存 (原始名和规范名都指向规范名，如果找到的话)
    zh_template_redirect_cache[clean_zh_name] = canonical_name
    if canonical_name and canonical_name != clean_zh_name:
        zh_template_redirect_cache[canonical_name] = canonical_name # 规范名指向自身

    return canonical_name

# --- 重要度评级定义 ---
IMPORTANCE_ORDER = {
    # 值越大越重要
    'top': 6,
    'high': 5,
    'mid': 4,
    'low': 3,
    'bottom': 2,
    'na': 1, # 不适用
    'no': 1, # 无重要度 (视为同级)
    # None 或其他视为最低
}

def get_importance_value(rating_str: str | None) -> int:
    """将重要度字符串转换为可比较的整数值"""
    if not rating_str:
        return 0
    return IMPORTANCE_ORDER.get(str(rating_str).strip().lower(), 0)

def compare_importance(en_rating: str | None, zh_rating: str | None) -> bool:
    """比较英文评级是否高于中文评级。True 表示英文更高"""
    en_value = get_importance_value(en_rating)
    zh_value = get_importance_value(zh_rating)
    # 只有当英文评级有效且严格大于中文评级时才认为更高
    return en_value > 0 and en_value > zh_value

# --- 解析函数 ---
def extract_en_wikiproject_templates(talk_page: pywikibot.Page) -> dict[str, str | None]:
    """
    从英文讨论页文本中提取相关的 WikiProject 模板名称及其 importance 参数。
    会查找页面顶层的模板和嵌套在第一个 WPBS 内的模板。
    排除 `excluded_en_projects_lower` 中的项目。
    返回一个字典 {模板名称: importance值 或 None}。
    """
    global error_en_talk_fetch, error_other
    relevant_en_templates = {} # 改为字典存储 {name: importance}
    try:
        # 存在性和重定向检查已移到 process_page 开头
        en_talk_text = talk_page.get()
        wikicode = mwparserfromhell.parse(en_talk_text)
        templates = wikicode.filter_templates()

        wpbs_processed = False
        for tpl in templates:
            tpl_name = str(tpl.name).strip().replace('_', ' ')
            tpl_name_lower = tpl_name.lower()

            # 提取 importance 的通用逻辑
            def get_tpl_importance(template_node):
                if template_node.has('importance', ignore_empty=True):
                    return str(template_node.get('importance').value).strip()
                return None

            # 检查是否是 WPBS
            if not wpbs_processed and tpl_name_lower in en_wpbs_names_lower:
                pywikibot.output(f"...找到英文 WPBS: {tpl_name}")
                wpbs_processed = True # 只处理第一个找到的 WPBS
                if tpl.has('1', ignore_empty=True):
                    param1_val = tpl.get('1').value
                    nested_wikicode = mwparserfromhell.parse(str(param1_val))
                    nested_templates = nested_wikicode.filter_templates()
                    for nested_tpl in nested_templates:
                        nested_tpl_name = str(nested_tpl.name).strip().replace('_', ' ')
                        nested_tpl_name_lower = nested_tpl_name.lower()
                        # 检查是否是 WikiProject 且不在排除列表
                        is_wp = nested_tpl_name_lower.startswith(('wikiproject ', 'wp '))
                        is_excluded = False
                        if is_wp:
                            project_name_part = nested_tpl_name_lower.split(' ', 1)[1] if ' ' in nested_tpl_name_lower else ''
                            full_project_name = f"wikiproject {project_name_part}"
                            is_excluded = full_project_name in excluded_en_projects_lower

                        if is_wp and not is_excluded:
                            importance = get_tpl_importance(nested_tpl)
                            # 如果模板已存在（可能顶层和WPBS内都有），优先保留有评级的
                            if nested_tpl_name not in relevant_en_templates or importance is not None:
                                relevant_en_templates[nested_tpl_name] = importance

            # 检查顶层模板是否是需要关注的 WikiProject (排除 WPBS 本身)
            elif tpl_name_lower not in en_wpbs_names_lower:
                 is_wp = tpl_name_lower.startswith(('wikiproject ', 'wp '))
                 is_excluded = False
                 if is_wp:
                     project_name_part = tpl_name_lower.split(' ', 1)[1] if ' ' in tpl_name_lower else ''
                     full_project_name = f"wikiproject {project_name_part}"
                     is_excluded = full_project_name in excluded_en_projects_lower

                 if is_wp and not is_excluded:
                     importance = get_tpl_importance(tpl)
                     # 如果模板已存在（可能顶层和WPBS内都有），优先保留有评级的
                     if tpl_name not in relevant_en_templates or importance is not None:
                         relevant_en_templates[tpl_name] = importance

    except APIError as e:
        pywikibot.error(f"...获取或解析英文讨论页 '{talk_page.title()}' 时发生 API 错误: {e}")
        error_en_talk_fetch += 1
    except Exception as e:
        pywikibot.error(f"...获取或解析英文讨论页 '{talk_page.title()}' 时发生未知错误: {e}")
        error_other += 1
        import traceback; traceback.print_exc()

    return relevant_en_templates # 返回字典

def get_existing_zh_banners(talk_page: pywikibot.Page) -> tuple[dict[str, tuple[str | None, mwparserfromhell.nodes.Template]], mwparserfromhell.nodes.Template | None, str, mwparserfromhell.wikicode.Wikicode | None]:
    """
    解析中文讨论页，获取第一个 WPBS 模板及其包含的专题横幅信息。
    返回:
    - 一个字典 {规范化横幅名称: (importance值 或 None, 对应的模板对象)}。
    - 第一个找到的 WPBS 的 mwparserfromhell 模板对象 (如果存在)。
    - 页面原始文本。
    - 解析后的 mwparserfromhell Wikicode 对象。
    """
    global error_zh_talk_fetch, error_other
    existing_banners_info = {} # 改为字典 {canonical_name: (importance, template_node)}
    zh_wpbs_template_obj = None
    original_text = ""
    wikicode = None

    try:
        if not talk_page.exists():
            pywikibot.output(f"...中文讨论页 '{talk_page.title()}' 不存在。")
            return existing_banners_info, None, "", None
        # 重定向检查已移到 process_page

        original_text = talk_page.get()
        wikicode = mwparserfromhell.parse(original_text) # 解析一次，后面复用

        for tpl in wikicode.filter_templates():
            tpl_name = str(tpl.name).strip().replace('_', ' ')
            tpl_name_lower = tpl_name.lower()

            # 寻找第一个 WPBS
            if tpl_name_lower in zh_wpbs_names_lower:
                pywikibot.output(f"...找到现有的中文 WPBS: {tpl_name}")
                zh_wpbs_template_obj = tpl # 保存 WPBS 对象引用
                if tpl.has('1', ignore_empty=True):
                    param1_val = tpl.get('1').value
                    # 解析参数1的内容来获取内部模板
                    # 注意：直接解析 param1.value 可能丢失原始格式，但对于提取模板和参数通常足够
                    nested_wikicode = mwparserfromhell.parse(str(param1_val))
                    nested_templates = nested_wikicode.filter_templates()
                    for nested_tpl in nested_templates:
                         nested_tpl_raw_name = str(nested_tpl.name).strip().replace('_', ' ')
                         canonical_name = get_canonical_zh_template_name(nested_tpl_raw_name)
                         if canonical_name:
                             importance = None
                             if nested_tpl.has('importance', ignore_empty=True):
                                 importance = str(nested_tpl.get('importance').value).strip()
                             # 存储规范名、重要度和模板节点本身
                             existing_banners_info[canonical_name] = (importance, nested_tpl)
                         # else:
                             # pywikibot.warning(f"...WPBS 内模板 '{nested_tpl_raw_name}' 无法获取规范名。")
                # 找到第一个 WPBS 后就停止查找其他模板
                break # <--- 重要：找到后退出循环

    except APIError as e:
        pywikibot.error(f"...获取或解析中文讨论页 '{talk_page.title()}' 时发生 API 错误: {e}")
        error_zh_talk_fetch += 1
        return {}, None, "", None # 出错时返回空
    except Exception as e:
        pywikibot.error(f"...获取或解析中文讨论页 '{talk_page.title()}' 时发生未知错误: {e}")
        error_other += 1
        import traceback; traceback.print_exc()
        return {}, None, "", None # 出错时返回空

    if zh_wpbs_template_obj:
         existing_names = sorted(existing_banners_info.keys())
         pywikibot.output(f"...已存在于第一个 WPBS 内的规范化专题模板 ({len(existing_names)}): {', '.join(existing_names)}")
         # for name, (imp, _) in existing_banners_info.items():
         #     pywikibot.output(f"    - {name}: importance={imp}") # 日志过多
    else:
         pywikibot.output("...未在页面上找到 WPBS 模板。")

    # 返回解析结果，包括 wikicode 对象供后续修改
    return existing_banners_info, zh_wpbs_template_obj, original_text, wikicode

# --- 主处理逻辑 ---
def process_page(en_title: str):
    """处理单个英文条目及其对应的中文条目"""
    global skipped_no_zh_page, skipped_no_en_talk, skipped_en_talk_redirect, skipped_zh_talk_redirect
    global skipped_no_relevant_en_banners, skipped_no_mapping, skipped_no_new_banners_or_importance_updates
    global skipped_creation_no_banners, error_zh_save, error_other, edits_made

    # 1. 获取中文页面对象
    zh_page = get_zh_page_from_en_title(en_title)
    if not zh_page: return

    # 2. 获取英文讨论页并提取相关模板
    en_page = pywikibot.Page(site_objects['en'], en_title)
    en_talk_page = en_page.toggleTalkPage()
    try: # 检查英文讨论页状态
        if not en_talk_page.exists():
             pywikibot.output(f"英文讨论页 '{en_talk_page.title()}' 不存在，跳过。")
             skipped_no_en_talk += 1
             return
        if en_talk_page.isRedirectPage():
             pywikibot.warning(f"英文讨论页 '{en_talk_page.title()}' 是重定向页，跳过。")
             skipped_en_talk_redirect += 1
             return
    except Exception as e:
         pywikibot.error(f"检查英文讨论页 '{en_talk_page.title()}' 状态时出错: {e}")
         error_other += 1
         return # 无法确定状态，跳过

    # 调用修改后的函数，获取英文模板及其重要度
    en_templates_with_importance = extract_en_wikiproject_templates(en_talk_page)
    if not en_templates_with_importance:
        pywikibot.output(f"未在英文讨论页 '{en_talk_page.title()}' 找到符合条件的专题模板，跳过。")
        skipped_no_relevant_en_banners += 1
        return
    pywikibot.output(f"从英文讨论页找到 {len(en_templates_with_importance)} 个相关模板及其评级:")
    # for name, imp in sorted(en_templates_with_importance.items()): # 日志过多
    #     pywikibot.output(f"  - {name}: importance={imp}")

    # 3. 映射英文模板到中文模板，并传递重要度信息
    # target_zh_templates_map = {zh_canonical_name: (en_importance, en_raw_name)}
    target_zh_templates_map = {}
    failed_mappings = set()
    for en_name, en_importance in en_templates_with_importance.items():
        zh_name_raw = get_zh_template_name_from_en(en_name)
        if zh_name_raw:
            canonical_zh_name = get_canonical_zh_template_name(zh_name_raw)
            if canonical_zh_name:
                # 如果同一个中文模板对应多个英文模板，优先保留评级更高的英文评级
                if canonical_zh_name not in target_zh_templates_map or \
                   compare_importance(en_importance, target_zh_templates_map[canonical_zh_name][0]):
                    target_zh_templates_map[canonical_zh_name] = (en_importance, zh_name_raw) # 存储英文评级和原始中文名
            else:
                pywikibot.warning(f"...映射得到的中文模板 '{zh_name_raw}' 无法获取规范名，忽略。")
                failed_mappings.add(en_name)
        else:
            failed_mappings.add(en_name)

    if not target_zh_templates_map:
        pywikibot.output(f"未能将任何英文模板成功映射到有效的中文模板，跳过页面 '{zh_page.title()}'。")
        skipped_no_mapping += 1
        return
    pywikibot.output(f"成功映射得到 {len(target_zh_templates_map)} 个目标中文模板(规范名)及其对应的英文评级:")
    # for name, (imp, _) in sorted(target_zh_templates_map.items()): # 日志过多
    #     pywikibot.output(f"  - {name}: en_importance={imp}")
    if failed_mappings: pywikibot.output(f"(注意: {len(failed_mappings)} 个英文模板未能映射或映射无效: {', '.join(sorted(list(failed_mappings)))})")


    # 4. 获取中文讨论页及现有横幅信息 (包括重要度和模板对象)
    zh_talk_page = zh_page.toggleTalkPage()
    try: # 检查中文讨论页是否是重定向
        if zh_talk_page.exists() and zh_talk_page.isRedirectPage():
             pywikibot.warning(f"中文讨论页 '{zh_talk_page.title()}' 是重定向页，跳过编辑。")
             skipped_zh_talk_redirect += 1
             return
    except Exception as e:
        pywikibot.error(f"检查中文讨论页 '{zh_talk_page.title()}' 状态时出错: {e}")
        error_other += 1
        return

    # 调用修改后的函数，获取现有横幅信息和 wikicode 对象
    existing_zh_banners_info, zh_wpbs_template_obj, original_zh_talk_text, wikicode = get_existing_zh_banners(zh_talk_page)
    page_exists = wikicode is not None # 如果 wikicode 不是 None，说明页面存在且已解析

    # --- 步骤 5 & 6: 处理模板添加和重要度更新 ---
    importance_updated = False
    templates_added = False

    if not wikicode: # 如果页面不存在，创建一个空的 wikicode 对象
        wikicode = mwparserfromhell.parse("")
        original_zh_talk_text = "" # 确保比较时是空字符串

    # 确定需要添加的新模板 (规范名)
    new_canonical_templates_to_add = set(target_zh_templates_map.keys()) - set(existing_zh_banners_info.keys())
    new_templates_data = [] # 存储 (原始名称, 英文评级)
    if new_canonical_templates_to_add:
        templates_added = True
        pywikibot.output(f"需要添加 {len(new_canonical_templates_to_add)} 个新模板(规范名): {', '.join(sorted(list(new_canonical_templates_to_add)))}")
        for canonical_name in sorted(list(new_canonical_templates_to_add)):
            en_importance, raw_zh_name = target_zh_templates_map[canonical_name]
            new_templates_data.append((raw_zh_name, en_importance)) # 使用映射时的原始中文名添加

    # 处理现有模板的重要性评级
    if zh_wpbs_template_obj and existing_zh_banners_info:
        pywikibot.output("检查现有中文模板的重要性评级...")
        # 确保我们操作的是 WPBS 参数 1 内的模板对象
        if zh_wpbs_template_obj.has('1'):
            param1_node = zh_wpbs_template_obj.get('1')
            param1_wikicode = param1_node.value # 这是 Wikicode 对象
            # 遍历参数1内的模板进行修改
            for nested_tpl in param1_wikicode.filter_templates():
                nested_tpl_raw_name = str(nested_tpl.name).strip().replace('_', ' ')
                canonical_name = get_canonical_zh_template_name(nested_tpl_raw_name)

                if canonical_name and canonical_name in existing_zh_banners_info:
                    current_zh_importance, _ = existing_zh_banners_info[canonical_name] # 从解析结果获取当前评级
                    target_en_importance, _ = target_zh_templates_map.get(canonical_name, (None, None)) # 获取对应的英文评级

                    new_importance = None
                    update_reason = ""

                    # 规则：仅当英文有评级且严格高于中文评级时才更新
                    if target_en_importance is not None and compare_importance(target_en_importance, current_zh_importance):
                        new_importance = target_en_importance
                        update_reason = f"英文评级 '{target_en_importance}' 高于中文评级 '{current_zh_importance}'"
                    # 其他情况（英文无评级、英文评级不高、中文无评级）均不更新或添加

                    if new_importance:
                        try:
                            # 设置值时不加空格，依赖 mwparserfromhell 的默认格式
                            param_value = new_importance
                            if nested_tpl.has('importance'):
                                # 更新现有参数
                                nested_tpl.get('importance').value = param_value
                            else:
                                # 添加为第一个参数
                                if nested_tpl.params:
                                    # 如果已有参数，插入到第一个参数之前
                                    first_param_name = nested_tpl.params[0].name
                                    # 使用 mwparserfromhell 的 add 方法，它通常会处理好空格
                                    nested_tpl.add('importance', param_value, before=first_param_name)
                                else:
                                    # 如果没有参数，直接添加
                                    nested_tpl.add('importance', param_value) # mwparserfromhell 会在模板名和 | 之间加空格

                            pywikibot.output(f"...更新模板 '{canonical_name}': {update_reason}")
                            importance_updated = True
                        except Exception as e:
                            pywikibot.error(f"!!! 更新模板 '{canonical_name}' 重要性时出错: {e}")
                            error_other += 1
        else:
             pywikibot.warning("...现有 WPBS 没有参数 '1'，无法检查内部模板的重要性。")


    # 如果没有新模板添加，也没有重要度更新，则跳过
    if not templates_added and not importance_updated:
        pywikibot.output("无需添加新模板，且现有模板重要性无需更新。跳过页面。")
        skipped_no_new_banners_or_importance_updates += 1
        return

    # --- 构建新文本 ---
    # (如果需要添加新模板)
    formatted_new_templates_list = []
    if templates_added:
        for raw_name, en_importance in new_templates_data:
            # 只有当英文版有评级时，才在新模板中加入 importance 参数
            if en_importance:
                # 确保模板名和第一个参数之间有空格
                formatted_new_templates_list.append(f"{{{{{raw_name} |importance={en_importance}}}}}")
            else:
                # 否则只加模板名
                formatted_new_templates_list.append(f"{{{{{raw_name}}}}}")

    if zh_wpbs_template_obj: # 合并到现有 WPBS
        pywikibot.output("处理现有 WPBS...")
        target_wpbs = zh_wpbs_template_obj

        # 如果有新模板要添加
        if templates_added:
            pywikibot.output("...将新模板添加到参数 1...")
            new_templates_str = "\n".join(formatted_new_templates_list)
            try:
                if target_wpbs.has('1'):
                    param1 = target_wpbs.get('1')
                    current_value_node = param1.value # 这是 Wikicode 对象
                    # 在现有内容的末尾（但在结束 }} 之前）添加新模板
                    # 为了保持格式，最好在最后一个现有模板后加换行再加新模板
                    current_value_str = str(current_value_node).strip()
                    if current_value_str: # 确保参数1内部有内容
                         # 在现有内容后加换行符和新模板字符串
                         current_value_node.append("\n" + new_templates_str)
                    else: # 参数1为空，直接设置
                         current_value_node.strip() # 清空可能存在的空白
                         current_value_node.append(new_templates_str)

                    # 确保参数值前后有换行符（可选，看风格）
                    # param1.value = "\n" + str(current_value_node).strip() + "\n"
                    pywikibot.output("...已将新模板追加到现有参数 1。")
                else: # 参数 1 不存在
                    param1_value = "\n" + new_templates_str + "\n"
                    added = False
                    if target_wpbs.has('class'):
                         try:
                             target_wpbs.add('1', param1_value, after='class')
                             added = True
                         except ValueError: pass
                    if not added:
                         target_wpbs.add('1', param1_value)
                    pywikibot.output("...参数 1 不存在，已创建并添加新模板。")
            except Exception as e:
                 pywikibot.error(f"!!! 添加新模板到现有 WPBS 时出错: {e}。")
                 error_other += 1
                 # 继续尝试保存，因为重要性可能已更新

        # 重要性更新已在上面通过修改 nested_tpl 完成，这里无需额外操作
        # 只需获取最终文本
        new_zh_talk_text = str(wikicode)

    else: # 创建新的 WPBS
        pywikibot.output("未检测到现有 WPBS，将创建新的 WPBS...")
        # 包含所有目标模板（包括原本就该有的，现在作为“新”模板添加）
        all_templates_data = []
        for canonical_name in sorted(list(target_zh_templates_map.keys())):
             en_importance, raw_zh_name = target_zh_templates_map[canonical_name]
             all_templates_data.append((raw_zh_name, en_importance))

        formatted_all_templates_list = []
        for raw_name, en_importance in all_templates_data:
            # 只有当英文版有评级时，才在新模板中加入 importance 参数
            if en_importance:
                # 确保模板名和第一个参数之间有空格
                formatted_all_templates_list.append(f"{{{{{raw_name} |importance={en_importance}}}}}")
            else:
                 # 否则只加模板名
                formatted_all_templates_list.append(f"{{{{{raw_name}}}}}")

        templates_for_new_wpbs = "\n".join(formatted_all_templates_list)
        new_wpbs_text = f"{{{{{default_zh_wpbs_name}|1=\n{templates_for_new_wpbs}\n}}}}"

        # 将新 WPBS 插入到讨论页顶部
        wikicode.insert(0, new_wpbs_text + "\n")

        new_zh_talk_text = str(wikicode).strip()

    # --- 步骤 7: 保存页面 ---
    # 只有当文本确实发生改变时才保存
    if new_zh_talk_text != original_zh_talk_text:
        # 构建动态编辑摘要
        summary_actions = []
        if templates_added:
            # 获取添加的模板名称列表（使用原始名称）
            added_names = sorted([data[0] for data in new_templates_data])
            summary_actions.append(f"+{'，'.join(added_names)}")
        if importance_updated:
            summary_actions.append("更新重要度") # 可以考虑更详细，但可能过长

        final_summary = edit_summary # Start with the base summary
        if summary_actions:
            # 使用分号分隔不同的操作类型
            final_summary += f"：{'; '.join(summary_actions)}"

        pywikibot.output("页面内容将发生变化:")
        pywikibot.showDiff(original_zh_talk_text, new_zh_talk_text)
        pywikibot.output(f"编辑摘要: {final_summary}") # 显示最终摘要

        if not dry_run:
            try:
                time.sleep(1)
                zh_talk_page.text = new_zh_talk_text
                # 使用动态生成的摘要
                zh_talk_page.save(summary=final_summary, botflag=use_bot_flag)
                edits_made += 1
                pywikibot.output("页面已成功保存。")
            except LockedPageError:
                pywikibot.error(f"!!! 页面 '{zh_talk_page.title()}' 被锁定，无法保存。")
                error_zh_save += 1
            except OtherPageSaveError as e:
                 pywikibot.error(f"!!! 保存页面 '{zh_talk_page.title()}' 时发生 OtherPageSaveError: {e}")
                 error_zh_save += 1
            except APIError as e:
                pywikibot.error(f"!!! 保存页面 '{zh_talk_page.title()}' 时发生 API 错误: {e}")
                error_zh_save += 1
                if "ratelimited" in str(e).lower():
                     pywikibot.warning("...触发速率限制，暂停 30 秒...")
                     time.sleep(30)
            except Exception as e:
                pywikibot.error(f"!!! 保存页面 '{zh_talk_page.title()}' 时发生未知错误: {e}")
                error_zh_save += 1
                import traceback; traceback.print_exc()
        else:
            pywikibot.output("Dry run 模式: 跳过保存。")
    else:
        # 检查为何文本未变
        if not page_exists and not new_zh_talk_text.strip(): # 页面原不存在且最终也为空
            pywikibot.output("页面不存在且最终无内容，跳过创建。")
            # skipped_creation_no_banners 计数器在前面已处理
        else:
             pywikibot.output("页面内容无变化（可能因已完成或处理错误），跳过保存。")
             # 如果 new_zh_templates_to_add_raw 不为空但文本没变，说明修改过程有问题
             if new_zh_templates_to_add_raw:
                 pywikibot.warning("...检测到需要添加新模板，但最终页面文本未改变，请检查修改逻辑或showDiff输出。")
             # skipped_no_new_banners 计数器在前面已处理

# --- 主函数 ---
def main():
    global processed_counter, edits_made, skipped_no_zh_page, skipped_no_en_talk
    global skipped_en_talk_redirect, skipped_zh_talk_redirect, skipped_no_relevant_en_banners
    global skipped_no_mapping, skipped_no_new_banners, skipped_creation_no_banners
    global error_en_talk_fetch, error_zh_talk_fetch, error_wd_fetch, error_map_fetch
    global error_zh_save, error_other
    global template_map_cache

    pywikibot.output("="*30)
    pywikibot.output("开始执行船舶专题模板同步机器人脚本")
    pywikibot.output(f"当前时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    pywikibot.output(f"Dry Run 模式: {'是' if dry_run else '否'}")
    pywikibot.output("="*30 + "\n")

    # 1. 初始化站点
    if not initialize_sites():
        return # 初始化失败，退出

    # 2. 加载缓存
    template_map_cache = load_cache(CACHE_FILE)

    # 3. 读取输入文件
    try:
        with open(json_file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # 假设格式是 {"rows": [["title1"], ["title2"], ...]}
        en_titles = [row[0] for row in data.get('rows', []) if row and isinstance(row, list) and len(row) > 0 and isinstance(row[0], str)]
        total_titles = len(en_titles)
        if not en_titles:
             pywikibot.error(f"错误：在文件 {json_file_path} 中未能找到有效的英文条目标题列表 (检查 'rows' 结构)。脚本将退出。")
             return
        pywikibot.output(f"从 {json_file_path} 加载了 {total_titles} 个英文条目标题。")
    except FileNotFoundError:
        pywikibot.error(f"错误：输入文件 {json_file_path} 未找到。脚本将退出。")
        return
    except json.JSONDecodeError:
        pywikibot.error(f"错误：文件 {json_file_path} 不是有效的 JSON 格式。脚本将退出。")
        return
    except Exception as e:
        pywikibot.error(f"读取输入文件 {json_file_path} 时发生错误: {e}")
        return

    # 4. 循环处理每个标题
    try:
        for i, en_title in enumerate(en_titles):
            processed_counter = i + 1
            pywikibot.output(f"\n--- [{processed_counter}/{total_titles}] 处理英文条目: {en_title} ---")
            try:
                process_page(en_title)
                # 可选：添加短暂延时以降低API请求频率
                # time.sleep(0.5)
            except Exception as e: # 捕获 process_page 内部未处理的意外错误
                 pywikibot.error(f"!!! 在处理 '{en_title}' 时发生顶层未知错误: {e}")
                 error_other += 1
                 import traceback; traceback.print_exc()
            finally:
                 # 可选：每处理 N 个页面保存一次缓存
                 if processed_counter % 50 == 0:
                    save_cache(template_map_cache, CACHE_FILE)
                 pass

    finally:
        # 5. 结束处理，保存缓存并打印统计信息
        pywikibot.output("\n" + "="*30)
        pywikibot.output("脚本处理完成。")
        pywikibot.output("正在保存最终的模板映射缓存...")
        save_cache(template_map_cache, CACHE_FILE)

        pywikibot.output("\n--- 统计信息 ---")
        pywikibot.output(f"总共尝试处理条目数: {processed_counter}")
        pywikibot.output(f"成功编辑页面数: {edits_made}")

        pywikibot.output("\n--- 跳过原因统计 ---")
        skipped_total = (skipped_no_zh_page + skipped_no_en_talk + skipped_en_talk_redirect +
                         skipped_zh_talk_redirect + skipped_no_relevant_en_banners + skipped_no_mapping +
                         skipped_no_new_banners_or_importance_updates + skipped_creation_no_banners) # 更新计数器名
        pywikibot.output(f"总跳过数: {skipped_total}")
        if skipped_no_zh_page: pywikibot.output(f"- 因无法找到对应中文页面或中文页无效/重定向: {skipped_no_zh_page}")
        if skipped_no_en_talk: pywikibot.output(f"- 因英文讨论页不存在: {skipped_no_en_talk}")
        if skipped_en_talk_redirect: pywikibot.output(f"- 因英文讨论页是重定向: {skipped_en_talk_redirect}")
        if skipped_zh_talk_redirect: pywikibot.output(f"- 因中文讨论页是重定向: {skipped_zh_talk_redirect}")
        if skipped_no_relevant_en_banners: pywikibot.output(f"- 因英文讨论页无相关专题模板: {skipped_no_relevant_en_banners}")
        if skipped_no_mapping: pywikibot.output(f"- 因未能将任何英文模板映射到有效的中文模板: {skipped_no_mapping}")
        if skipped_no_new_banners_or_importance_updates: pywikibot.output(f"- 因无需添加新模板且重要性无需更新: {skipped_no_new_banners_or_importance_updates}") # 更新描述
        if skipped_creation_no_banners: pywikibot.output(f"- 因中文讨论页不存在且无需添加模板而跳过创建: {skipped_creation_no_banners}")


        pywikibot.output("\n--- 错误统计 ---")
        error_total = (error_en_talk_fetch + error_zh_talk_fetch + error_wd_fetch +
                       error_map_fetch + error_zh_save + error_other)
        pywikibot.output(f"总错误数: {error_total}")
        if error_wd_fetch: pywikibot.output(f"- 获取 Wikidata 信息时出错: {error_wd_fetch}")
        if error_map_fetch: pywikibot.output(f"- 查询模板映射时出错: {error_map_fetch}")
        if error_en_talk_fetch: pywikibot.output(f"- 获取/解析英文讨论页时出错: {error_en_talk_fetch}")
        if error_zh_talk_fetch: pywikibot.output(f"- 获取/解析中文讨论页时出错: {error_zh_talk_fetch}")
        if error_zh_save: pywikibot.output(f"- 保存中文讨论页时出错: {error_zh_save}")
        if error_other: pywikibot.output(f"- 其他/未知处理错误: {error_other}")

        pywikibot.output("="*30)
        pywikibot.stopme() # 提示 Pywikibot 脚本结束

# --- 脚本入口 ---
if __name__ == '__main__':
    main()