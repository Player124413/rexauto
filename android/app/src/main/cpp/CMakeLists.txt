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

if(CMAKE_BUILD_TYPE STREQUAL "Release")
  set(CMAKE_INTERPROCEDURAL_OPTIMIZATION ON)
endif()

add_subdirectory("${REXSDK_DIR}" rexglue-sdk)

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
target_compile_options(recomp PRIVATE
    $<$<CXX_COMPILER_ID:Clang,AppleClang>:-gline-tables-only>
    -Wno-unused-variable -Wno-unused-label -Wno-unused-but-set-variable)
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
target_link_options(main PRIVATE -Wl,--export-dynamic)
add_dependencies(main rexgpu-xenos)
set_target_properties(main PROPERTIES CXX_VISIBILITY_PRESET default VISIBILITY_INLINES_HIDDEN OFF)
