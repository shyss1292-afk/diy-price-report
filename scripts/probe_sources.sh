#!/bin/bash
# 数据源可达性探测
# 用途：在编写真实平台适配器之前，先确认目标站点是否可访问、是否触发反爬。
# 用法：./scripts/probe_sources.sh

UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

probe() {
  local name="$1" url="$2"
  local body="$TMP/body.html"
  local meta
  meta=$(curl -sS -o "$body" -w "%{http_code}|%{size_download}|%{time_total}" \
        -m 20 -A "$UA" -H "Accept-Language: zh-CN,zh;q=0.9" -L "$url" 2>/dev/null) || meta="000|0|0"

  local code size time
  IFS='|' read -r code size time <<< "$meta"

  local flags=""
  grep -qiE "验证码|人机验证|安全验证|滑动验证|请完成验证" "$body" 2>/dev/null && flags="$flags [验证码]"
  grep -qiE "captcha|recaptcha|geetest|risk|forbidden|access denied" "$body" 2>/dev/null && flags="$flags [风控关键词]"
  grep -qE "¥|￥|价格|price" "$body" 2>/dev/null && flags="$flags [含价格文本]"
  grep -qE "GBK|gb2312|gbk" "$body" 2>/dev/null && flags="$flags [GBK编码]"

  printf "  %-12s HTTP=%-4s 大小=%-9s 耗时=%-7s%s\n" \
    "$name" "$code" "${size}B" "${time}s" "${flags:-  [无异常标记]}"
}

echo "数据源可达性探测  $(date '+%Y-%m-%d %H:%M:%S')"
echo "---------------------------------------------------------------"
echo "【电商平台搜索页】"
probe "京东搜索"   "https://search.jd.com/Search?keyword=RTX5070"
probe "拼多多搜索" "https://mobile.yangkeduo.com/search_result.html?search_key=RTX5070"
probe "淘宝搜索"   "https://s.taobao.com/search?q=RTX5070"
probe "苏宁易购"   "https://search.suning.com/RTX5070/"
echo "【二手平台】"
probe "闲鱼H5"     "https://h5.m.goofish.com/"
probe "转转"       "https://www.zhuanzhuan.com/"
echo "【历史价格站】"
probe "慢慢买"     "https://www.manmanbuy.com/"
probe "什么值得买" "https://www.smzdm.com/"
echo "---------------------------------------------------------------"
echo "说明：出现 [验证码] / [风控关键词] 说明该源需要浏览器渲染或登录态才能采集。"
