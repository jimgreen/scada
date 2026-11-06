# c104 2.2.1 确认等待修复

在 IEC104 本机回环测试中，同一条遥控只发送了一次；收到了肯定的 ACT_CON，旧客户端仍偶发返回超时。2.2.1 的等待函数先等待通知，再检查结果；如果确认先到，通知会丢失。这里保留项目原先的 2.2.1，移植[上游修复 7f4b22d](https://github.com/Fraunhofer-FIT-DIEN/iec104-python/commit/7f4b22d1ec71b450ace8a3c0a384a1e8db756e09)：在持锁期间先检查已有结果，只有还在等待时才睡眠。没有增加重发，也没有把否定确认或超时转换成成功。另为 Windows 原生编译分开静态库和 DLL 导入库文件名，避免 Ninja 的输出冲突。

从项目根目录执行 `python -m pip install -r requirements.txt`。本目录提供 Windows x64 / CPython 3.11 的 wheel 和包含原始许可证、绑定及编译依赖源代码的 sdist。其他平台从 sdist 编译，需要适用的本机编译器与 Python 开发文件。项目此次实测平台是 Windows，未在 Linux 实机上编译或运行。

复现源包：从 [PyPI c104 2.2.1](https://pypi.org/project/c104/2.2.1/#files) 下载原始 `c104-2.2.1.tar.gz`，运行 `python vendor/c104/backport.py <原始压缩包>`。脚本校验原始 SHA-256，要求目标代码精确匹配；输出可重复生成。构建 wheel：

```text
python -m pip wheel --no-deps --wheel-dir vendor/c104 vendor/c104/c104-2.2.1+scada.1.tar.gz
```

Windows 构建使用 x64 Visual Studio 开发者环境、Windows SDK，并设置 `CMAKE_GENERATOR=Ninja`。上游包与补丁来源均注明在此；完整源包保留上游许可证。
