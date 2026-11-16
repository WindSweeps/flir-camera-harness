# Source and rebuild

Runtime adds auditable Python modules before the original PyInstaller main. It
preserves the original bootloader and every existing archive entry byte for byte.
The reviewed additions are in `src/`.

Build with Python 3.10+ on Windows (standard library only), from the package root:

```powershell
python source/build.py --patch "C:/Data/FLIR_ROI_Camera_Patch_1.1.zip" --output runtime/FLIR_ROI_Camera.exe
```

The supplied 1.1 patch is an input, not included again in this package. Its hash is
checked by build.py. Keep runtime/_internal from the included original distribution.
Run verify.cmd for native Qt/SDK-mock self-tests without a separate Python install.

Optional developer tests with a host Python:

```powershell
python source/tests/harness_protocol_test.py
python source/tests/harness_stdio_test.py --exe runtime/FLIR_ROI_Camera.exe --output tests-run
python source/tests/harness_sdk_test.py --exe runtime/FLIR_ROI_Camera.exe --output tests-sdk
```

The SDK test additionally requires official `mcp==2.3.0` and `jsonschema`. Wire tests
use only the host standard library and launch the bundled runtime in replay mode.
Test output directories must be new to check no-overwrite behavior. Physical
devices are never enumerated/opened by these tests. Tests exercise actual tools
and original GUI/backend controllers, plus deliberately injected SDK node mocks.

`qrcodegen.py` carries its original MIT notice. Existing bundled third-party runtime
licenses and files are retained without modification in `runtime/_internal`.
