import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    kotlin("jvm")
}

// Android (minSdk 26) uruchamia ten kod na Javie 17, więc kompilujemy tylko z jej API.
kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
        freeCompilerArgs.add("-Xjdk-release=17")
        allWarningsAsErrors.set(true)
        optIn.add("kotlinx.coroutines.ExperimentalCoroutinesApi") // runTest, advanceUntilIdle, currentTime w testach
    }
}

tasks.withType<JavaCompile> {
    options.release.set(17)
}

dependencies {
    api("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.10.2") // StateFlow i CoroutineScope są w API kontrolera
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.9.0")

    testImplementation(kotlin("test"))
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.10.2")
}

tasks.test {
    useJUnitPlatform()
    testLogging {
        events("failed")
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
    }
}
