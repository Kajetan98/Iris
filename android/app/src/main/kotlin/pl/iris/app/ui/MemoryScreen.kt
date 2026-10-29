package pl.iris.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
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
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import pl.iris.core.Conversation
import pl.iris.core.Fact
import pl.iris.core.Source
import pl.iris.core.Texts

/** "Co IRIS o mnie wie": fakty z pewnością i usuwanie pojedynczego faktu (spec, sekcja 14). */
@Composable
fun MemoryScreen(conversation: Conversation, onBack: () -> Unit) {
    var facts by remember { mutableStateOf<List<Fact>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    suspend fun load() {
        try {
            facts = withContext(Dispatchers.IO) { conversation.facts() }
                .sortedWith(compareBy<Fact> { it.source != Source.STATED }.thenByDescending { it.confidence })
            error = null
        } catch (_: Exception) {
            error = "Nie udało się wczytać listy."
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
        Text("Co IRIS o mnie wie", color = IrisColors.Ink, fontFamily = FontFamily.Serif, fontSize = 24.sp)
        Text(
            "Fakty podane wprost są pewne. Przypuszczenia z rozmów zyskują pewność, gdy się powtarzają, i IRIS pyta o nie. " +
                "Naucz ją czegoś, mówiąc: zapamiętaj, że ...",
            color = IrisColors.Ink2,
            fontSize = 14.sp,
        )
        val current = facts
        when {
            error != null -> Text(error.orEmpty(), color = IrisColors.Warn)
            current == null -> Text("Wczytuję listę.", color = IrisColors.Ink2)
            current.isEmpty() -> Text("IRIS jeszcze nic o tobie nie wie.", color = IrisColors.Ink2)
            else -> LazyColumn(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                items(current, key = { it.id }) { fact ->
                    Row(
                        Modifier
                            .fillMaxWidth()
                            .border(1.dp, IrisColors.Line, RoundedCornerShape(12.dp))
                            .padding(12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                    ) {
                        Column(Modifier.weight(1f)) {
                            Text(fact.text, color = IrisColors.Ink)
                            Text(Texts.factMeta(fact), color = IrisColors.Ink2, fontSize = 14.sp)
                        }
                        OutlinedButton(onClick = {
                            scope.launch {
                                withContext(Dispatchers.IO) { conversation.forget(fact.id) }
                                load()
                            }
                        }) { Text("Usuń") }
                    }
                }
            }
        }
        OutlinedButton(onClick = onBack) { Text("Zamknij") }
    }
}
