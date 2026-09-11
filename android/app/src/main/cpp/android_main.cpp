/**
 * rexauto Android port - native entry point (generic, title-agnostic).
 *
 * Derived from deivid22srk/hells-gate-recomp-android (android_main.cpp) and
 * generalised: the project identifier arrives from CMake (REX_APP_NAME), the
 * game data root and the graphics settings are written by the Java launcher
 * (SetupActivity) into the app's external files dir:
 *
 *   <external>/game_root.txt   absolute path of the folder holding default.xex
 *   <external>/settings.txt    one "key=value" per line -> passed as --key=value
 *                              cvars (resolution_scale, vsync, present_effect...)
 *
 * SDL3's SDL_main.h maps main() to SDL_main; org.libsdl.app.SDLActivity calls
 * SDL_RunApp on a dedicated thread which lands here.
 */

#include <SDL3/SDL_main.h>
#include <SDL3/SDL_system.h>

#include <android/log.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <signal.h>
#include <ucontext.h>
#include <unistd.h>
#include <unwind.h>
#include <sys/resource.h>
#include <jni.h>

#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <filesystem>
#include <fstream>
#include <memory>
#include <string>
#include <vector>

#include <fmt/format.h>

#include "android_gamepad.h"
#if REX_HAVE_ADRENOTOOLS
#include <adrenotools/driver.h>
#endif
#include <rex/cvar.h>
#include <rex/filesystem.h>
#include <rex/logging.h>
#include <rex/main_android.h>
#include <rex/memory.h>
#include <rex/platform.h>
#include <rex/thread.h>
#include <rex/ui/windowed_app.h>
#include <rex/ui/windowed_app_context_sdl.h>

#if REX_PLATFORM_ANDROID

#ifndef REX_APP_NAME
#error "REX_APP_NAME must be defined by the build (the rexglue project name)"
#endif

namespace {

constexpr char kAppIdentifier[] = REX_APP_NAME;
constexpr char kConfigFileName[] = "game_root.txt";
constexpr char kSettingsFileName[] = "settings.txt";
// <internal files>/gpu_driver/<name>/  + driver.txt naming the .so (written by
// the launcher after importing a Turnip/Adreno driver zip). Internal storage,
// not external: dlopen() refuses libraries on world-writable paths.
constexpr char kDriverDirName[] = "gpu_driver";
constexpr char kDriverConfigName[] = "driver.txt";

#define ALOGE(...) __android_log_print(ANDROID_LOG_ERROR, REX_APP_NAME, __VA_ARGS__)
#define ALOGI(...) __android_log_print(ANDROID_LOG_INFO, REX_APP_NAME, __VA_ARGS__)

std::string ReadTrimmedFile(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) return {};
  std::string line;
  std::getline(in, line);
  while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
  return line;
}

// settings.txt -> ["--key=value", ...]. Blank lines and '#' comments ignored.
std::vector<std::string> ReadSettingsArgs(const std::string& path) {
  std::vector<std::string> out;
  std::ifstream in(path, std::ios::binary);
  if (!in) return out;
  std::string line;
  while (std::getline(in, line)) {
    while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
    if (line.empty() || line[0] == '#') continue;
    const auto eq = line.find('=');
    if (eq == std::string::npos || eq == 0) continue;
    if (line.rfind("env.", 0) == 0) {
      // env.NAME=value -> setenv(NAME, value); consumed by getenv() readers
      // such as the tolerant dispatcher (REX_HEAL_DISCOVER).
      setenv(line.substr(4, eq - 4).c_str(), line.substr(eq + 1).c_str(), 1);
      continue;
    }
    out.emplace_back("--" + line);
  }
  return out;
}

std::string QueryNativeLibraryDir() {
  Dl_info info{};
  if (dladdr(reinterpret_cast<void*>(&QueryNativeLibraryDir), &info) && info.dli_fname) {
    std::string path(info.dli_fname);
    const size_t slash = path.find_last_of('/');
    if (slash != std::string::npos) return path.substr(0, slash);
  }
  return {};
}

JavaVM* QueryJavaVm() {
  auto* env = static_cast<JNIEnv*>(SDL_GetAndroidJNIEnv());
  if (env) {
    JavaVM* vm = nullptr;
    if (env->GetJavaVM(&vm) == JNI_OK && vm) return vm;
  }
  return nullptr;
}

// --- crash reporter ---------------------------------------------------------
// Android throws a native crash away (a tombstone the user cannot reach without
// adb). This writes <logs>/crash.txt from the signal handler with the
// async-signal-safe subset: signal, fault address, pc, the symbol (sub_XXXXXXXX
// = the guest function) and module offset of every frame. SetupActivity shows
// the file on the next launch. The SDK's own handlers run first (write-watch,
// SEH regions) and chain here only for genuinely unhandled faults.
char g_crash_path[512];

void CrashWrite(int fd, const char* s) {
  size_t n = 0;
  while (s[n]) ++n;
  while (n) {
    ssize_t w = write(fd, s, n);
    if (w <= 0) return;
    s += w;
    n -= static_cast<size_t>(w);
  }
}

void CrashHex(int fd, uint64_t v) {
  char buf[17];
  int i = 16; buf[i] = 0;
  do { int d = int(v & 15); buf[--i] = char(d < 10 ? '0' + d : 'a' + d - 10); v >>= 4; } while (v);
  CrashWrite(fd, buf + i);
}

void CrashAddr(int fd, uintptr_t pc) {
  CrashWrite(fd, "0x");
  CrashHex(fd, pc);
  Dl_info di{};
  if (dladdr(reinterpret_cast<void*>(pc), &di) && di.dli_fname) {
    const char* base = di.dli_fname;
    for (const char* q = base; *q; ++q) if (*q == '/') base = q + 1;
    CrashWrite(fd, "  ");
    CrashWrite(fd, base);
    CrashWrite(fd, "+0x");
    CrashHex(fd, pc - reinterpret_cast<uintptr_t>(di.dli_fbase));
    if (di.dli_sname) {
      CrashWrite(fd, "  ");
      CrashWrite(fd, di.dli_sname);
      CrashWrite(fd, "+0x");
      CrashHex(fd, pc - reinterpret_cast<uintptr_t>(di.dli_saddr));
    }
  }
}

struct CrashTrace { int fd; int n; };

_Unwind_Reason_Code CrashUnwind(struct _Unwind_Context* c, void* arg) {
  auto* t = static_cast<CrashTrace*>(arg);
  uintptr_t pc = _Unwind_GetIP(c);
  if (pc && t->n < 48) {
    CrashWrite(t->fd, "  #");
    char idx[4] = {char('0' + t->n / 10), char('0' + t->n % 10), ' ', 0};
    CrashWrite(t->fd, idx);
    CrashAddr(t->fd, pc);
    CrashWrite(t->fd, "\n");
  }
  ++t->n;
  return t->n >= 48 ? _URC_END_OF_STACK : _URC_NO_REASON;
}

void CrashHandler(int sig, siginfo_t* info, void* uctx) {
  static volatile sig_atomic_t entered = 0;
  if (!entered) {
    entered = 1;
    int fd = open(g_crash_path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd >= 0) {
      CrashWrite(fd, REX_APP_NAME " native crash\nsignal: ");
      const char* name = sig == SIGSEGV ? "SIGSEGV (access violation)" :
                         sig == SIGBUS ? "SIGBUS (misaligned/bad mapping)" :
                         sig == SIGILL ? "SIGILL (illegal instruction / trap)" :
                         sig == SIGFPE ? "SIGFPE (arithmetic)" :
                         sig == SIGABRT ? "SIGABRT (abort / assertion / uncaught exception)" :
                         sig == SIGTRAP ? "SIGTRAP (breakpoint)" : "unknown";
      CrashWrite(fd, name);
      CrashWrite(fd, "\nfault address: 0x");
      CrashHex(fd, reinterpret_cast<uintptr_t>(info ? info->si_addr : nullptr));
      auto* uc = static_cast<ucontext_t*>(uctx);
      if (uc) {
        CrashWrite(fd, "\npc: ");
        CrashAddr(fd, static_cast<uintptr_t>(uc->uc_mcontext.pc));
        CrashWrite(fd, "\nlr: ");
        CrashAddr(fd, static_cast<uintptr_t>(uc->uc_mcontext.regs[30]));
        CrashWrite(fd, "\nsp: 0x");
        CrashHex(fd, uc->uc_mcontext.sp);
        CrashWrite(fd, "\nx0-x7:");
        for (int i = 0; i < 8; ++i) { CrashWrite(fd, " 0x"); CrashHex(fd, uc->uc_mcontext.regs[i]); }
        CrashWrite(fd, "\nx19-x21:");
        for (int i = 19; i < 22; ++i) { CrashWrite(fd, " 0x"); CrashHex(fd, uc->uc_mcontext.regs[i]); }
      }
      CrashWrite(fd, "\nbacktrace (pc  module+offset  symbol+offset; sub_XXXXXXXX = guest function):\n");
      CrashTrace t{fd, 0};
      _Unwind_Backtrace(CrashUnwind, &t);
      CrashWrite(fd, "\n");
      close(fd);
    }
  }
  signal(sig, SIG_DFL);
  raise(sig);
}

void InstallCrashReporter(const std::string& log_dir) {
  std::string path = log_dir + "/crash.txt";
  if (path.size() >= sizeof(g_crash_path)) return;
  std::memcpy(g_crash_path, path.c_str(), path.size() + 1);
  // Run on an alternate stack so a stack overflow is reported too.
  static std::vector<char> alt_stack(1 << 17);
  stack_t ss{};
  ss.ss_sp = alt_stack.data();
  ss.ss_size = alt_stack.size();
  sigaltstack(&ss, nullptr);
  struct sigaction sa{};
  sa.sa_sigaction = CrashHandler;
  sa.sa_flags = SA_SIGINFO | SA_ONSTACK;
  sigemptyset(&sa.sa_mask);
  for (int sig : {SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT, SIGTRAP}) sigaction(sig, &sa, nullptr);
  std::set_terminate([] {
    // Uncaught C++ exception (REX_UNIMPLEMENTED, bad_alloc...): name it, then
    // fall into the SIGABRT path above for the backtrace.
    if (auto ex = std::current_exception()) {
      try { std::rethrow_exception(ex); }
      catch (const std::exception& e) {
        __android_log_print(ANDROID_LOG_ERROR, REX_APP_NAME, "uncaught exception: %s", e.what());
        int fd = open(g_crash_path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
        if (fd >= 0) { CrashWrite(fd, "uncaught exception: "); CrashWrite(fd, e.what()); CrashWrite(fd, "\n"); close(fd); }
      } catch (...) {}
    }
    std::abort();
  });
}

std::string ResolveLogDir(const std::string& external_dir) {
  const std::string dir = external_dir + "/logs";
  std::error_code ec;
  std::filesystem::create_directories(dir, ec);
  return dir;
}

// Load a user-supplied Vulkan driver (Turnip / newer Adreno blob) through
// libadrenotools and publish the handle for the SDK's DynamicLibrary (see the
// android-custom-vulkan SDK patch). Silent no-op when nothing is configured.
void LoadCustomVulkanDriver(const std::string& internal_dir, const std::string& lib_dir) {
#if REX_HAVE_ADRENOTOOLS
  const std::string cfg = internal_dir + "/" + kDriverDirName + "/" + kDriverConfigName;
  const std::string spec = ReadTrimmedFile(cfg);  // "<subdir>/<libvulkan_freedreno.so>"
  if (spec.empty()) return;
  const auto slash = spec.rfind('/');
  const std::string dir = internal_dir + "/" + kDriverDirName + "/" +
                          (slash == std::string::npos ? "" : spec.substr(0, slash)) + "/";
  const std::string so = slash == std::string::npos ? spec : spec.substr(slash + 1);
  std::error_code ec;
  if (!std::filesystem::is_regular_file(dir + so, ec)) {
    ALOGE("custom GPU driver configured but %s%s is missing - using the system driver", dir.c_str(), so.c_str());
    return;
  }
  const std::string tmp = internal_dir + "/" + kDriverDirName + "/tmp";
  std::filesystem::create_directories(tmp, ec);
  void* handle = adrenotools_open_libvulkan(RTLD_NOW, ADRENOTOOLS_DRIVER_CUSTOM, tmp.c_str(),
                                            lib_dir.c_str(), dir.c_str(), so.c_str(), nullptr, nullptr);
  if (!handle) {
    ALOGE("adrenotools_open_libvulkan(%s%s) failed - using the system driver", dir.c_str(), so.c_str());
    return;
  }
  setenv("REX_VULKAN_HANDLE", std::to_string(reinterpret_cast<uintptr_t>(handle)).c_str(), 1);
  ALOGI("custom Vulkan driver loaded: %s%s", dir.c_str(), so.c_str());
#else
  (void)internal_dir; (void)lib_dir;
#endif
}

int RunAndroidApp() {
  // Foreground apps may lower their own nice value down to -10 without any
  // permission; the guest threads inherit it. Ignored (EACCES) if refused.
  setpriority(PRIO_PROCESS, 0, -10);
  const std::string lib_dir = QueryNativeLibraryDir();
  JavaVM* java_vm = QueryJavaVm();
  if (lib_dir.empty()) ALOGE("nativeLibraryDir unresolved - GPU plugin loading will fail");

  if (!SDL_InitSubSystem(SDL_INIT_VIDEO)) {
    ALOGE("SDL_InitSubSystem(SDL_INIT_VIDEO) failed: %s", SDL_GetError());
    return EXIT_FAILURE;
  }
  const char* external_c = SDL_GetAndroidExternalStoragePath();
  std::string external_dir = external_c ? external_c : "";
  if (external_dir.empty()) {
    external_dir = std::string("/storage/emulated/0/Android/data/com.rexauto.port.") +
                   kAppIdentifier + "/files";
  }

  const std::string game_root = ReadTrimmedFile(external_dir + "/" + kConfigFileName);
  const char* internal_c = SDL_GetAndroidInternalStoragePath();
  LoadCustomVulkanDriver(internal_c ? internal_c : "", lib_dir);
  std::error_code ec;
  std::filesystem::create_directories(external_dir + "/data", ec);
  const std::string log_dir = ResolveLogDir(external_dir);
  // A previous crash.txt is consumed by the launcher; a fresh launch means the
  // user has seen it (or chose to play again) -- start clean.
  std::filesystem::remove(log_dir + "/crash.txt", ec);
  InstallCrashReporter(log_dir);

  rex::SetAndroidApplicationContext(java_vm, SDL_GetAndroidActivity(), lib_dir.c_str());
  rex::thread::AndroidInitialize();
  rex::memory::AndroidInitialize();
  rex::filesystem::AndroidInitialize();

  // The port's src/<name>_app.h carries rexauto's desktop "portable paths"
  // hook (game in <exe>/assets, saves in <exe>/userdata). On Android the exe
  // folder is the read-only nativeLibraryDir, so keep the explicit roots below
  // authoritative: the hook honours these two env toggles.
  setenv("REX_NO_PORTABLE_USERDATA", "1", 1);
  unsetenv("REX_PORTABLE_ONLY");

  std::vector<std::string> args;
  args.emplace_back(kAppIdentifier);
  if (!game_root.empty()) {
    args.emplace_back(fmt::format("--game_data_root={}", game_root));
  } else {
    ALOGE("no game_root.txt under %s - re-run setup", external_dir.c_str());
  }
  args.emplace_back(fmt::format("--user_data_root={}", external_dir + "/data"));
  args.emplace_back(fmt::format("--log_file={}/{}.log", log_dir, kAppIdentifier));
  // Mobile defaults (see hells-gate-recomp-android docs/android_performance):
  // no per-frame full re-upload of guest memory pages; the write-watch keeps
  // coherency. Everything below can be overridden by settings.txt.
  args.emplace_back("--clear_memory_page_state=false");
  args.emplace_back("--fullscreen=true");
  // GPU emulation plugin. On Windows the rexauto SDK auto-discovers
  // rexgpu-*.dll beside the exe; on Android there is no such scan, and without
  // it the kernel logs "no GPU emulation loaded (gpu_plugin not set)" and the
  // title renders nothing. librexgpu-xenos.so ships in the APK; the SDK
  // resolves "xenos" against nativeLibraryDir (SetAndroidApplicationContext).
  args.emplace_back("--gpu_plugin=xenos");
  // Mobile pacing defaults (all overridable from settings.txt): the guest
  // thread scheduler must not try to pin threads to the 360's 6 hardware
  // threads (we have big.LITTLE and want the kernel to migrate freely), and
  // the vsync worker should sleep, not spin.
  args.emplace_back("--ignore_thread_affinities=true");
  args.emplace_back("--ignore_thread_priorities=true");
  args.emplace_back("--log_flush_interval=5");  // batch log writes
  for (auto& a : ReadSettingsArgs(external_dir + "/" + kSettingsFileName)) {
    ALOGI("setting: %s", a.c_str());
    args.emplace_back(std::move(a));
  }

  std::vector<char*> argv_ptrs;
  argv_ptrs.reserve(args.size());
  for (auto& arg : args) argv_ptrs.push_back(arg.data());

  auto remaining = rex::cvar::Init(static_cast<int>(argv_ptrs.size()), argv_ptrs.data());
  (void)remaining;
  rex::cvar::ApplyEnvironment();
  rex::InitLoggingEarly();

  REXLOG_INFO("android_main: app={} external_dir={} game_root={}", kAppIdentifier,
              external_dir, game_root);

  int result;
  {
    rex::ui::SDLWindowedAppContext app_context;
    if (!app_context.Initialize()) {
      REXLOG_ERROR("SDLWindowedAppContext::Initialize failed: {}", SDL_GetError());
      return EXIT_FAILURE;
    }
    const auto creator = rex::ui::WindowedApp::GetCreator(kAppIdentifier);
    if (!creator) {
      REXLOG_ERROR("app '{}' is not registered - recompiled code built for another name",
                   kAppIdentifier);
      return EXIT_FAILURE;
    }
    std::unique_ptr<rex::ui::WindowedApp> app = creator(app_context);
    if (app->OnInitialize()) {
      rexport::gamepad::EnsureVirtualPadAttached();
      result = app_context.RunMainMessageLoop();
    } else {
      REXLOG_ERROR("OnInitialize failed - see earlier errors");
      result = EXIT_FAILURE;
    }
    app->InvokeOnDestroy();
  }
  REXLOG_INFO("android_main: exiting with code {}", result);
  return result;
}

}  // namespace

int main(int argc, char* argv[]) {
  (void)argc;
  (void)argv;
  return RunAndroidApp();
}

#endif  // REX_PLATFORM_ANDROID
