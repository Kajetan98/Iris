package pl.iris.core

/** Hasło wybudzające (docs/spec.md, sekcja 14): rozpoznawanie jest w aplikacji, tu tylko reguły. */

private val NOT_LETTERS = Regex("[^\\p{L}]+")

/** Prawda, gdy w rozpoznanym tekście jest słowo zaczynające się od "iris" lub "irys" ("Hej Iris", "Irysku"). */
fun matchesWakeWord(text: String): Boolean =
    text.lowercase().split(NOT_LETTERS).any { it.startsWith("iris") || it.startsWith("irys") }

/** Czy nasłuch hasła ma teraz działać; w każdym innym stanie mikrofon musi być wolny. */
fun wakeListening(enabled: Boolean, modelInstalled: Boolean, micGranted: Boolean, foreground: Boolean, phase: Phase): Boolean =
    enabled && modelInstalled && micGranted && foreground && phase == Phase.IDLE
