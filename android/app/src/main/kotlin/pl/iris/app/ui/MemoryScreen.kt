package pl.iris.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import pl.iris.core.Conversation
import pl.iris.core.MEMORY_DOMAINS
import pl.iris.core.MemoryView
import pl.iris.core.Texts

/** "Pamięć IRIS": pojęcia od najsilniejszych z usuwaniem pojedynczym i liczbą zapomnianych (spec, sekcja 14). */
@Composable
fun MemoryScreen(conversation: Conversation, exportNotice: String?, onExport: () -> Unit, onBack: () -> Unit) {
    var view by remember { mutableStateOf<MemoryView?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    suspend fun load() {
        try {
            view = withContext(Dispatchers.IO) { conversation.memory() }
            error = null
        } catch (_: Exception) {
            error = "Nie udało się wczytać pamięci."
        }
    }
    LaunchedEffect(Unit) { load() }

    Column(
        Modifier
            .fillMaxSize()
            .background(IrisColors.Bg)
            .safeDrawingPadding()
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Pamięć IRIS", color = IrisColors.Ink, fontFamily = FontFamily.Serif, fontSize = 24.sp)
        Text(
            "IRIS pamięta pojęcia z rozmów. Pojęcie rośnie, gdy wraca w rozmowie, i słabnie, gdy nie wraca, aż zostaje zapomniane. " +
                "Liczby i dane wrażliwe nie są zapisywane. Naucz ją czegoś, mówiąc: zapamiętaj, że ...",
            color = IrisColors.Ink2,
            fontSize = 14.sp,
        )
        val current = view
        when {
            error != null -> Text(error.orEmpty(), color = IrisColors.Warn)
            current == null -> Text("Wczytuję pamięć.", color = IrisColors.Ink2)
            current.concepts.isEmpty() -> Text("IRIS jeszcze nic nie pamięta.", color = IrisColors.Ink2)
            else -> LazyColumn(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                items(current.concepts, key = { it.concept.key }) { item ->
                    Row(
                        Modifier
                            .fillMaxWidth()
                            .border(1.dp, IrisColors.Line, RoundedCornerShape(12.dp))
                            .padding(12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                    ) {
                        // Kolor domeny jak na kuli pamięci w przeglądarce.
                        Box(Modifier.size(10.dp).background(domainColor(item.domain), CircleShape))
                        Column(Modifier.weight(1f)) {
                            Text(item.concept.label, color = IrisColors.Ink)
                            Text(Texts.conceptMeta(item), color = IrisColors.Ink2, fontSize = 14.sp)
                        }
                        OutlinedButton(onClick = {
                            scope.launch {
                                withContext(Dispatchers.IO) { conversation.forget(item.concept.key) }
                                load()
                            }
                        }) { Text("Usuń") }
                    }
                }
            }
        }
        if (current != null && current.forgotten > 0) {
            Text("Zapomniane z czasem: ${current.forgotten}.", color = IrisColors.Ink2, fontSize = 14.sp)
        }
        if (!current?.concepts.isNullOrEmpty()) {
            Text(
                "Eksport zapisuje całą pamięć do pliku JSON, który wskażesz. Plik nie jest szyfrowany i zawiera dane osobiste, więc trzymaj go w bezpiecznym miejscu.",
                color = IrisColors.Ink2,
                fontSize = 14.sp,
            )
            if (exportNotice != null) Text(exportNotice, color = IrisColors.Ink)
            OutlinedButton(onClick = onExport) { Text("Eksportuj do pliku") }
        }
        OutlinedButton(onClick = onBack) { Text("Zamknij") }
    }
}

private fun domainColor(id: String): Color {
    val rgb = (MEMORY_DOMAINS.firstOrNull { it.id == id } ?: MEMORY_DOMAINS.last()).rgb
    return Color(rgb[0], rgb[1], rgb[2])
}
