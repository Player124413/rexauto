cmake_minimum_required(VERSION 3.25)
project(rexauto_android LANGUAGES C CXX ASM)

# Handed over from Gradle (-D... in build.gradle.kts), with fallbacks for a
# direct CMake invocation.
set(REX_PROJECT_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../../.." CACHE PATH "rexauto checkout")
set(REXSDK_DIR "${REX_PROJECT_ROOT}/android/sdk/rexglue-sdk" CACHE PATH "patched ReXGlue SDK tree")
set(REX_PORT_DIR "" CACHE PATH "rexauto port dir (autoports/<name>/port)")
set(REX_APP_NAME "game" CACHE STRING "rexglue project name")

if(NOT EXISTS "${REXSDK_DIR}/CMakeLists.txt")
    message(FATAL_ERROR "ReXGlue SDK not found at ${REXSDK_DIR} - run tools/android_sdk.sh first")
endif()
if(NOT EXISTS "${REX_PORT_DIR}/generated/default/sources.cmake")
    message(FATAL_ERROR "no generated code at ${REX_PORT_DIR}/generated/default - run the desktop pipeline (rexauto.py --until build) first")
endif()

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

set(REXGLUE_ENABLE_TRACY OFF CACHE BOOL "" FORCE)
set(REXGLUE_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(REXGLUE_USE_VULKAN ON CACHE BOOL "" FORCE)
set(REXGLUE_USE_D3D12 OFF CACHE BOOL "" FORCE)

# --- mobile performance flags --------------------------------------------------
# Applied to everything below (SDK runtime, GPU plugin, recompiled code):
#   * -O3 + thin LTO across the runtime; the SDK adds -O3 itself for Release,
#     repeated here so the flag also reaches Gradle-injected configs.
#   * ARMv8.2-A + fp16/dotprod: every 64-bit Android SoC since ~2018 (Snapdragon
#     660+, Exynos 9810+, Kirin 970+, Dimensity/Helio G) has it; the hot loops
#     (byte-swapping guest memory, vector ops emitted for VMX128) benefit.
#     Gated by REX_ANDROID_BASELINE_ARM=1 to fall back to plain armv8-a.
#   * -fno-plt / -fomit-frame-pointer: fewer indirections per call. The
#     recompiled code is millions of small functions calling through the
#     dispatcher; per-call overhead is the CPU bottleneck on phones.
#   * -fno-semantic-interposition / hidden visibility: lets LTO inline across
#     the runtime library instead of going through the GOT.
if(CMAKE_BUILD_TYPE STREQUAL "Release")
  set(CMAKE_INTERPROCEDURAL_OPTIMIZATION ON)
  set(CMAKE_INTERPROCEDURAL_OPTIMIZATION_RELEASE ON)
  add_compile_options(-O3 -DNDEBUG -fomit-frame-pointer -fno-plt
                      -fno-semantic-interposition -fmerge-all-constants
                      -fno-stack-protector -fno-unwind-tables -fno-asynchronous-unwind-tables)
  if(NOT DEFINED ENV{REX_ANDROID_BASELINE_ARM})
    add_compile_options(-march=armv8.2-a+fp16+dotprod -mtune=cortex-a76)
  endif()
  add_link_options(-Wl,-O2 -Wl,--gc-sections -Wl,--icf=all -Wl,--as-needed -Wl,-z,now)
endif()

add_subdirectory("${REXSDK_DIR}" rexglue-sdk)

# --- libadrenotools: custom Turnip/Adreno Vulkan driver loader ---------------
# Ships as libadrenotools.so + its hook libs (libmain_hook.so, libhook_impl.so,
# libfile_redirect_hook.so, libgsl_alloc_hook.so); android_main.cpp dlopen()s
# the driver through it when the launcher has one configured.
set(ADRENOTOOLS_DIR "${REXSDK_DIR}/../libadrenotools" CACHE PATH "libadrenotools checkout")
if(EXISTS "${ADRENOTOOLS_DIR}/CMakeLists.txt")
    set(BUILD_SHARED_LIBS ON)
    add_subdirectory("${ADRENOTOOLS_DIR}" adrenotools)
    set(REX_HAVE_ADRENOTOOLS ON)
else()
    message(WARNING "libadrenotools not found at ${ADRENOTOOLS_DIR} - custom GPU drivers disabled")
endif()

# --- recompiled guest code (from the desktop rexauto pipeline) ---------------
set(REX_GEN_DIR "${REX_PORT_DIR}/generated/default")
include("${REX_GEN_DIR}/sources.cmake")
set(REX_GEN_SOURCES "")
foreach(src ${GENERATED_SOURCES})
    if(IS_ABSOLUTE "${src}")
        list(APPEND REX_GEN_SOURCES "${src}")
    else()
        list(APPEND REX_GEN_SOURCES "${REX_GEN_DIR}/${src}")
    endif()
endforeach()

add_library(recomp STATIC ${REX_GEN_SOURCES})
target_include_directories(recomp PRIVATE "${REX_PORT_DIR}" "${REX_PORT_DIR}/src" "${REX_GEN_DIR}")
target_precompile_headers(recomp PRIVATE "${REX_GEN_DIR}/${REX_APP_NAME}_pch.h")
# The generated PPC->C++ is enormous (hundreds of MB of source): strip debug
# info entirely, keep -O3. (No -fno-exceptions: REX_UNIMPLEMENTED in the
# generated PCH throws.) No RTTI: nothing in it uses dynamic_cast/typeid.
target_compile_options(recomp PRIVATE
    -g0 -O3
    -fno-rtti
    -fno-math-errno -fno-trapping-math
    -Wno-unused-variable -Wno-unused-label -Wno-unused-but-set-variable
    -Wno-unused-value -Wno-parentheses-equality)
# Data-section relocation pressure: the register/init tables are huge.
set_target_properties(recomp PROPERTIES INTERPROCEDURAL_OPTIMIZATION OFF)
target_link_libraries(recomp PUBLIC rex::runtime rexui)

# --- app library loaded by SDLActivity ----------------------------------------
add_library(main SHARED
    android_gamepad.cpp
    android_main.cpp
    "${REX_PORT_DIR}/src/main.cpp"
    "${REXSDK_DIR}/src/ui/rex_app.cpp"
)
target_include_directories(main PRIVATE
    "${REX_PORT_DIR}" "${REX_PORT_DIR}/src" "${REX_GEN_DIR}"
    "${REXSDK_DIR}/include" "${REXSDK_DIR}/src/ui")
target_compile_options(main PRIVATE -Wno-unused-variable)
target_compile_definitions(main PRIVATE
    REXGLUE_BUILD_CONFIG="$<CONFIG>"
    REX_APP_NAME="${REX_APP_NAME}")
target_link_libraries(main PRIVATE recomp rex::runtime rexui)
if(REX_HAVE_ADRENOTOOLS)
    target_compile_definitions(main PRIVATE REX_HAVE_ADRENOTOOLS=1)
    target_link_libraries(main PRIVATE adrenotools)
    add_dependencies(main main_hook file_redirect_hook gsl_alloc_hook hook_impl)
endif()
target_link_options(main PRIVATE -Wl,--export-dynamic)
add_dependencies(main rexgpu-xenos)
set_target_properties(main PROPERTIES CXX_VISIBILITY_PRESET default VISIBILITY_INLINES_HIDDEN OFF)
