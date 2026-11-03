#!/usr/bin/env python3
"""Host contract test compiling the ACTUAL AOSP17 HIDL getAudioPortImpl source.

Uses minimal platform mocks; not a replacement for Android native compilation or VTS.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEVICE = ROOT / "audio/core/all-versions/default/Device.cpp"
BEGIN = "template <typename HalPort>\nReturn<void> Device::getAudioPortImpl("
END = "\n#if MAJOR_VERSION <= 6"

PREFIX = r"""
#include <cassert>
#include <functional>
#include <iostream>

using status_t = int;
constexpr status_t NO_ERROR = 0;

enum class Result { OK, NOT_SUPPORTED };
struct Void {};
template<typename T> struct Return {};
template<> struct Return<void> {
    Return(Void) {}
};
struct AudioPort { int value = 0; };
struct HalPort { int value = 0; };
struct audio_hw_device_t {};

struct HidlUtils {
    static int toStatus;
    static int fromStatus;
    static status_t audioPortToHal(const AudioPort& src, HalPort* dst) {
        dst->value = src.value;
        return toStatus;
    }
    static status_t audioPortFromHal(const HalPort& src, AudioPort* dst) {
        dst->value = src.value;
        return fromStatus;
    }
};
int HidlUtils::toStatus = NO_ERROR;
int HidlUtils::fromStatus = NO_ERROR;

Result analyzeStatus(const char*, int status) {
    return status == NO_ERROR ? Result::OK : Result::NOT_SUPPORTED;
}
using getAudioPort_cb = std::function<void(Result, const AudioPort&)>;
class Device {
  public:
    audio_hw_device_t* mDevice = nullptr;
    template<typename T>
    Return<void> getAudioPortImpl(
        const AudioPort& port, getAudioPort_cb cb,
        int (*halGetter)(audio_hw_device_t*, T*), const char* halGetterName);
};
"""

SUFFIX = r"""
static int gGetterCount = 0;
int fakeGetter(audio_hw_device_t*, HalPort* port) {
    ++gGetterCount;
    port->value = 91;
    return NO_ERROR;
}

int main() {
    Device d;
    AudioPort in{42};
    int callbacks = 0;
    AudioPort observed{0};
    Result observedResult = Result::OK;
    auto listener = [&](Result r, const AudioPort& p) {
        ++callbacks;
        observed = p;
        observedResult = r;
    };

    // Null function pointer: no segfault, callback exactly once, original port.
    d.getAudioPortImpl<HalPort>(in, listener, nullptr, "get_audio_port");
    assert(callbacks == 1 && observedResult == Result::NOT_SUPPORTED);
    assert(observed.value == 42 && gGetterCount == 0);

    // Implemented method: still routes to HAL and converts returned port.
    callbacks = 0;
    d.getAudioPortImpl<HalPort>(in, listener, &fakeGetter, "get_audio_port");
    assert(callbacks == 1 && observedResult == Result::OK);
    assert(observed.value == 91 && gGetterCount == 1);

    // Failure before invoking HAL: still invokes callback once.
    callbacks = 0;
    HidlUtils::toStatus = 1;
    d.getAudioPortImpl<HalPort>(in, listener, &fakeGetter, "get_audio_port");
    assert(callbacks == 1 && observedResult == Result::NOT_SUPPORTED);
    assert(gGetterCount == 1);

    HidlUtils::toStatus = 0;
    HidlUtils::fromStatus = 1;
    callbacks = 0;
    d.getAudioPortImpl<HalPort>(in, listener, &fakeGetter, "get_audio_port");
    assert(callbacks == 1 && observedResult == Result::NOT_SUPPORTED);
    assert(gGetterCount == 2);
    std::cout << "PASS: source-extracted HIDL getAudioPort contract" << std::endl;
}
"""


class AudioPortGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = DEVICE.read_text()
        cls.assert_source = source
        if source.count(BEGIN) != 1:
            raise AssertionError("Expected exactly one AOSP17 getAudioPortImpl definition")
        start = source.index(BEGIN)
        stop = source.index(END, start)
        cls.body = source[start:stop]
        if "if (halGetter == nullptr)" not in cls.body:
            raise AssertionError("Missing null function pointer guard")

    def test_guard_precedes_conversion_and_hal_call(self):
        text = self.body
        guard = text.index("if (halGetter == nullptr)")
        conversion = text.index("HidlUtils::audioPortToHal")
        call = text.index("halGetter(mDevice, &halPort)")
        self.assertLess(guard, conversion)
        self.assertLess(guard, call)
        early = text[guard:conversion]
        self.assertIn("_hidl_cb(Result::NOT_SUPPORTED, port);", early)
        self.assertIn("return Void();", early)

    def test_compiled_actual_function_on_host(self):
        compiler = shutil.which("g++") or shutil.which("clang++")
        self.assertIsNotNone(compiler, "C++ compiler required by CI")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            cpp = p / "test.cpp"
            binary = p / "test"
            cpp.write_text(PREFIX + self.body + SUFFIX)
            subprocess.run(
                [compiler, "-std=c++17", "-O0", "-Wall", "-Wextra",
                 "-Werror", str(cpp), "-o", str(binary)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run([str(binary)], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
