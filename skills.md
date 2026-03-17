
---

请为我安装如下skill

## name: 

mx_select_stock (妙想智能选股skill)

## description: 

本 Skill 支持基于股票选股条件，如行情**指标、财务指标等**，筛选满足条件的股票；可查询**指定行业 / 板块内的股票、上市公司**，以及**板块指数的成分股**；同时支持**股票、上市公司、板块 / 指数推荐**等相关任务，采用此skill可避免大模型在选股时使用了过时信息。


---

# 妙想智能选股skill  (mx_select_stock)

通过**自然语言查询**进行选股（ 类型支持 **\['A股', '港股', '美股'\]**），将返回的 **datalist** 按 **columns** 把英文列名替换为中文后，输出**全量数据**的 CSV 及对应的数据说明文件。

\
## 使用方式


1. 在妙想Skills页面获取apikey
2. 将apikey存到环境变量，命名为MX_APIKEY，检查本地api是否存在，若存在可直接用。
3. 使用post请求如下接口，务必使用post请求。
4. 编写调用方式脚本

```javascript
curl -X POST --location 'https://mkapi2.dfcfs.com/finskillshub/api/claw/stock-screen' \
--header 'Content-Type: application/json' \
--header 'apikey:mkt_8vo8r72N5h6odbc0CD2wIuACcaF-kV7bzqPcyfNkk8M' \
--data '{"keyword": "今日涨幅2%的股票", "pageNo": 1, "pageSize": 20}'
```

### 接口结果释义

## 一、顶层核心状态 / 统计字段

 

表格

|字段路径|类型|核心释义|
|----|----|----|
|`status`|数字|接口全局状态，0 = 成功|
|`message`|字符串|接口全局提示，ok = 成功|
|`data.code`|字符串|选股业务层状态码，100 = 解析成功|
|`data.msg`|字符串|选股业务层提示|
|`data.data.resultType`|数字|结果类型枚举，2000 为标准选股结果|
|`data.data.result.total`|数字|【核心】选股结果总数量（符合条件的股票数）|
|`data.data.result.totalRecordCount`|数字|与 total 一致，结果总条数，做数据校验用|

### 2.1 列定义：`data.data.result.columns`（数组）

 

核心作用：定义表格每一列的展示规则、属性、业务键，是前端渲染表格列的依据，数组中每个对象对应表格的一列，与`dataList`的行数据键一一映射，核心子字段如下：

 

表格

|子字段|类型|核心释义|
|----|----|----|
|`title`|字符串|表格列展示标题（如最新价 (元)、涨跌幅 (%)）|
|`key`|字符串|【核心】列唯一业务键，与`dataList`中对象的键映射（如 NEWEST_PRICE、CHG）|
|`dateMsg`|字符串|列数据对应的日期（如 2026.03.12）|
|`sortable`|布尔|该列是否支持前端排序|
|`sortWay`|字符串|默认排序方式（desc = 降序 /asc = 升序）|
|`redGreenAble`|布尔|该列数值是否支持红绿涨跌着色（涨红跌绿）|
|`unit`|字符串|列数值单位（元、%、股、倍）|
|`dataType`|字符串|列数据类型（String/Double/Long），用于前端渲染格式|

 

### 2.2 行数据：`data.data.result.dataList`（数组）

 

核心作用：选股结果的具体股票数据，数组中每个对象对应一只符合条件的股票，是表格的行数据；对象的键与`columns`中的`key`严格映射，值为该股票对应列的实际数据，核心业务键（列）释义如下：

 

表格

|核心键|数据类型|核心释义|
|----|----|----|
|`SERIAL`|字符串|表格行序号|
|`SECURITY_CODE`|字符串|股票代码（如 603866、300991）|
|`SECURITY_SHORT_NAME`|字符串|股票简称（如桃李面包、创益通）|
|`MARKET_SHORT_NAME`|字符串|市场简称（SH = 上交所，SZ = 深交所）|
|`NEWEST_PRICE`|数字 / 字符串|最新价（单位：元）|
|`CHG`|数字 / 字符串|涨跌幅（单位：%）|
|`PCHG`|数字 / 字符串|涨跌额（单位：元）|

## 三、选股条件 / 统计相关字段

 

该部分为选股的条件说明、结果统计，展示选股的筛选规则及各条件匹配的股票数量，核心路径均在`data.data`下：

 

表格

|字段路径|类型|核心释义|
|----|----|----|
|`responseConditionList`|数组|【核心】单条筛选条件的统计，每个对象对应 1 个筛选条件，含条件描述、匹配股票数|
|`responseConditionList[].describe`|字符串|筛选条件描述（如今日涨跌幅在 \[1.5%,2.5%\] 之间）|
|`responseConditionList[].stockCount`|数字|该条件匹配的股票数量|
|`totalCondition`|对象|【核心】组合筛选条件的总统计，即所有条件叠加后的最终筛选规则|
|`totalCondition.describe`|字符串|组合条件描述（如今日涨跌幅在 \[1.5%,2.5%\] 之间 且 股票代码）|
|`totalCondition.stockCount`|数字|组合条件匹配的股票数量（与 result.total 一致）|
|`parserText`|字符串|选股条件的解析文本，以分号分隔单条件（如今日涨跌幅在 \[1.5%,2.5%\] 之间；股票代码）|

## 数据结果为空

提示用户到东方财富妙想AI进行选股。