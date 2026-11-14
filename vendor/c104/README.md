# c104 2.2.1+scada.3 命令修复与 SOE 确认发送

在 IEC104 本机回环测试中，同一条遥控只发送了一次；收到了肯定的 ACT_CON，旧客户端仍偶发返回超时。2.2.1 的等待函数先等待通知，再检查结果；如果确认先到，通知会丢失。这里保留项目原先的 2.2.1，移植[上游修复 7f4b22d](https://github.com/Fraunhofer-FIT-DIEN/iec104-python/commit/7f4b22d1ec71b450ace8a3c0a384a1e8db756e09)：在持锁期间先检查已有结果，只有还在等待时才睡眠。没有增加重发，也没有把否定确认或超时转换成成功。另为 Windows 原生编译分开静态库和 DLL 导入库文件名，避免 Ninja 的输出冲突。

从项目根目录执行 `python -m pip install -r requirements.txt`。本目录提供 Windows x64 / CPython 3.11 的 wheel 和包含原始许可证、绑定及编译依赖源代码的 sdist。其他平台从 sdist 编译，需要适用的本机编译器与 Python 开发文件。项目此次实测平台是 Windows，未在 Linux 实机上编译或运行。

复现源包：从 [PyPI c104 2.2.1](https://pypi.org/project/c104/2.2.1/#files) 下载原始 `c104-2.2.1.tar.gz`，运行 `python vendor/c104/backport.py <原始压缩包>`。脚本校验原始 SHA-256，要求目标代码精确匹配；输出可重复生成。构建 wheel：

```text
python -m pip wheel --no-deps --wheel-dir vendor/c104 vendor/c104/c104-2.2.1+scada.3.tar.gz
```

scada.2 继续保留上述等待修复，并通过可审查的 [command_contracts.patch](command_contracts.patch) 修正命令报文：

- 撤销只发送一次 COT=8，不再重新选择；客户端只接受相应的肯定 COT=9。COT=7 或否定确认不能被当作撤销成功。
- 服务端按请求原因返回 ACTCON=7 或 DEACTCON=9；选择执行的 ACTTERM 从实际执行请求生成，选择位为 0。
- 选择过期、取消或连接关闭只释放选择，不生成成功执行结束帧。失败执行也不生成成功 ACTTERM。
- 执行和撤销核对连接、发起地址、类型、IOA、值及限定词；S/E 和 CP56 时间字段不参与选值比较。同连接重新选择更新保存的命令，不能借另一连接的选择执行。
- SELECT_AND_EXECUTE 已完成或已过期后再撤销，返回否定 DEACTCON；这是无有效选择的明确拒绝，不会重新选择或再次下发执行。DIRECT 的撤销仍由应用处理回调决定是否接受。
- 服务端高、低优先级 ASDU 队列从各 100 项扩大为各 1024 项，避免本轮 8192 点单次批量上送超过队列容量而覆盖尚未发送的数据。两客户端连续两批上送及随后总召均校验完整值与质量。队列仍有上限；慢连接、持续超载或更大突发仍需流量控制，不能据此宣称无限缓冲或 SOE 无损。增加的缓冲占用随连接数增长。

原始 2.2.1 源包经 SHA-256 校验后先应用等待修复，再严格匹配补丁上下文；源码不匹配时停止，不做模糊合并。Windows x64 / CPython 3.11 的新 wheel 和 sdist 均保存在本目录，旧 scada.1 包保留用于历史复现。requirements.txt 固定新版本。

Windows 构建使用 x64 Visual Studio 开发者环境、Windows SDK、CMake 3.27.9 与 Ninja，并设置 `CMAKE_GENERATOR=Ninja`。先初始化 VS 环境，再将隔离环境的 Scripts 加到更新后的 PATH；在同一 cmd 行中应使用延迟变量展开，避免覆盖编译器路径。上游包与补丁来源均注明在此；完整源包保留上游许可证。

本轮测试使用 `output/iec104-native-20261114/venv`，不覆盖其他 Python 环境的已加载扩展。部署时须在实际启动 C/S/T 的解释器中安装项目依赖并重启对应进程。代码和 wheel 的测试通过不代表已经切换生产进程。

修改前的 SOE 复验：100/1024 是按最大 ASDU 大小估算缓冲区的容量参数，并不是小报文的固定条数上限。旧低优先级队列在延迟 ACK 的 16384 条短报文突发中只收到 8452 条；历史证据保留在 `docs/iec104_soe_acceptance_20261114.html`。

scada.3 在 scada.2 后精确应用 [soe_delivery.patch](soe_delivery.patch)：

- `Server.submit_soe(asdus)` 只接受长度正确、COT=3 的 CP56 单点/双点 ASDU，每批最多 512 个 ASDU，直接保留时间原始字节。已有未确认批次时拒绝叠加提交。
- SOE 使用不会覆盖旧条目的高优先级队列；部分提交失败也必须先轮询完成状态，随后重试整个数据库批次。
- `Server.poll_soe()` 返回 0（等待）、1（原接收连接全部 APCI 确认）或 -1（连接变化/部分失败，必须重试）。在同一发送锁下检查高优先级队列和未确认 I 帧，连接生命周期编号防止旧连接地址复用被误认为成功。
- Python S/T 使用数据库持久事件及游标，每通道仅保留一个有界在途批次。C 在原生点接收回调中按 ASDU 完整事务写入事件；同一报文内重复 IOA 的事件不会被最新值缓存合并。

这是有明确边界的“至少一次”转发：确认后、游标提交前崩溃可能重复；新连接不会自动重放已确认历史；APCI 确认不等于对端业务持久化成功。C 数据库写入失败会记录 SOE 错误并让工作线程退出当前连接，但外部主站/从站的传输确认无法代替业务事务确认，不能宣称任意故障下绝对不丢。

CP56 字节原样保留，项目事件表按 UTC 日历解释显示。原生 pybind 的公开 datetime 转换仍忽略输入时区并返回本地 naive datetime；SOE 路径绕开该转换，并未改变所有 c104 日期 API 的契约。CP24 仍不支持。事件自动清理未启用，需结合项目保留周期制定归档策略；未确认游标对应历史不能提前清理。

scada.3 源包 SHA-256：`1c726a6222eed92e4b8ffec05598aead51f858e3a444b35923c050a4b50a8ddf`。Windows CPython 3.11 wheel SHA-256：`c155c22fa62076168c3466d2c024b43152022ad2e9e24aa76a4e3bf4f589f773`。上述开发验收使用隔离环境；发布时须在实际启动环境安装并重启服务。
