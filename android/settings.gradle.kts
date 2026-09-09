pluginManagement {
    repositories {
        // Region mirror for local builds only (enable with -PaliyunMirror=1).
        // Never enabled on CI: the mirror answers 502 occasionally and Gradle
        // aborts metadata resolution instead of falling through.
        if (providers.gradleProperty("aliyunMirror").isPresent) {
            maven("https://maven.aliyun.com/repository/google")
            maven("https://maven.aliyun.com/repository/public")
        }
        google {
            content {
                includeGroupByRegex("com\\.android.*")
                includeGroupByRegex("com\\.google.*")
                includeGroupByRegex("androidx.*")
            }
        }
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        if (providers.gradleProperty("aliyunMirror").isPresent) {
            maven("https://maven.aliyun.com/repository/google")
            maven("https://maven.aliyun.com/repository/public")
        }
        google()
        mavenCentral()
    }
}

rootProject.name = "rexauto-android"
include(":app")
