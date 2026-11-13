# marc-ingest

馆藏交换记录的字节级结构化接收、原件留存与书目身份管理。

## 当前能力

`marc.py` 严格按 ISO 2709 声明的字节边界解析 MARC21：leader、目录、控制字段、
数据字段、指标位、重复子字段与重复字段分别表示，每一项都带原始字节偏移、
长度、摘要和十六进制切片。字段定位只使用 leader 和目录项声明的长度与起点，
不使用解码后的字符数。

* leader 声明的记录长度与其末字节 0x1d 必须同时成立，否则外层边界不可信，
  解析立即停止并给出证据，绝不向后扫描结束符猜测。
* 外层边界可信时，单条记录内部的目录/字段错误标记该记录 `invalid`，批量继续
  处理下一条有可靠声明长度的记录。
* 只接受 leader/09 = `a`（UTF-8）。其它字符集整条隔离为
  `unsupported_encoding`；损坏的 UTF-8 报 `invalid_utf8` 并保留原始字节，
  不用 U+FFFD 替换字符掩盖。
* 控制号身份是 `(来源, 003 机构代码, 001 控制号)`：不同馆的同号记录互不覆盖。

`storage.py` 把交付批次、书目身份和原件修订分为三层保存：

* 交付批次 `(来源, sha256(整批字节))`：重复导入同一批字节得到同一收件记录。
* 书目身份 `(来源, 003, 001)`：同一身份下出现新摘要记为 `revision`（同控制号
  的新内容），出现相同摘要记为 `resend`（原件重送），两者都保留原始字节与解析
  结果。
* 缺 001 的记录记为 `unassigned`，损坏或字符集不符的记录只保留解析证据，
  不写修订。

`source.py` 提供批次概览检查，`sampledata.py` 构造可重复的虚构夹具，
`transport.py` 提供独立回环 HTTP 演练源。解释范围见 `docs/source-contract.md`。

## 环境

Python 3.11 或以上，仅标准库，无需凭据和外部数据。使用容器时可执行
`docker build -t marc-ingest .`，依赖及镜像须在正式运行前准备。
运行入口、测试与演示均不安装包，不访问公网。数据库使用临时路径，不向代码
目录写入业务文件。

## 测试与构建

构建工具版本固定在 `requirements-build.txt`，在环境准备阶段执行
`python3 -m pip install -r requirements-build.txt`；容器构建已包含此步骤。
应用运行仍仅使用标准库，测试及演示不安装依赖。

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q app.py marc.py source.py sampledata.py storage.py transport.py
python3 -m build --wheel --no-isolation
```

## 快速演示

```sh
python3 app.py demo
python3 app.py showcase
```

`demo` 通过本地 HTTP 来源领取同一批次两次、一份修订和一份原件重送，保存到临时
数据库，输出收件身份、修订/重送判定、中文标题字节长度与重复责任者。
`showcase` 输出中文长度、重复字段/子字段与截断记录的实际解析结果。
两者都真实执行，退出后自动清理，正常在数秒内完成。

## 单独检查、结构化与导出

```sh
python3 app.py fixtures /tmp/marc-ingest-fixtures
python3 app.py inspect /tmp/marc-ingest-fixtures/sample.mrc
python3 app.py structure /tmp/marc-ingest-fixtures/repeated.mrc
python3 app.py capture /tmp/marc-ingest-fixtures/sample.mrc --database /tmp/marc-ingest.sqlite3 --source local-partner
python3 app.py list --database /tmp/marc-ingest.sqlite3
python3 app.py export --database /tmp/marc-ingest.sqlite3 --delivery 1
```

`structure` 输出完整的 leader、目录、字段、指标位、子字段和每段的原始偏移证据。
`export` 从已保存的修订导出书名、责任者与出版信息，每项都带字段/子字段位置，
可以回到原始记录中的出现位置。生成夹具不会覆盖已有文件。运行错误以 JSON 输出
到标准错误并返回退出码 2；外层边界不可信时不生成任何收件，内部单条损坏则保留
证据并继续。内部原件与任何对外展示副本应分开管理，不写入真实个人数据。
