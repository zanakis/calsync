package io.github.zanakis.calsync

import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneOffset
import java.time.temporal.ChronoUnit

data class Event(
    val id: String,
    val tag: String?,
    val start: String,
    val end: String,
    val showAs: String,
    val isAllDay: Boolean,
    val isCancelled: Boolean,
    val iCalUId: String? = null,
    val subject: String? = null,
) {
    fun key(source: String) =
        BlockKey(source, start, end, showAs, isAllDay && start.endsWith("T00:00:00") && end.endsWith("T00:00:00"))
}

data class BlockKey(val source: String, val start: String, val end: String, val showAs: String, val allDay: Boolean)

class Plan(val create: List<BlockKey>, val delete: List<String>)

private val showAsStrength = listOf("oof", "busy", "tentative", "workingElsewhere", "free", "unknown")

private fun strength(showAs: String) = showAsStrength.indexOf(showAs).let { if (it < 0) showAsStrength.size else it }

/**
 * Groups copies of one meeting found in several calendars: same time, and same iCalUId or subject.
 * Returns the accounts holding each meeting and the single block every other account should get.
 * Must pick the same block as merge_meetings in calsync.py, or the Mac and the phone undo each other's blocks.
 */
fun meetings(sources: Map<String, List<Event>>): List<Pair<Set<String>, BlockKey>> {
    val bySlot = mutableMapOf<Triple<String, String, Boolean>, MutableList<Pair<Set<String>, List<BlockKey>>>>()
    for ((oid, events) in sources) for (ev in events) {
        val key = ev.key(oid)
        val ids = setOfNotNull(ev.iCalUId?.let { "uid:$it" }, ev.subject?.let { "subject:$it" })
        val groups = bySlot.getOrPut(Triple(key.start, key.end, key.allDay)) { mutableListOf() }
        val hits = groups.filter { (it.first intersect ids).isNotEmpty() }
        groups.removeAll(hits)
        groups += (ids + hits.flatMap { it.first }) to (listOf(key) + hits.flatMap { it.second })
    }
    return bySlot.values.flatten().map { (_, keys) ->
        keys.map { it.source }.toSet() to keys.minWith(compareBy<BlockKey>({ strength(it.showAs) }, { it.source }))
    }
}

fun plan(calendars: Map<String, List<Event>>, mirror: Set<String>, sendOnly: Set<String> = emptySet()): Map<String, Plan> {
    val meetings = meetings(calendars.mapValues { (_, events) ->
        events.filter { it.tag == null && !it.isCancelled && it.showAs in mirror }
    })
    return calendars.mapValues { (target, events) ->
        val wanted = if (target in sendOnly) emptyMap<BlockKey, Int>()
        else meetings.filter { target !in it.first }.groupingBy { it.second }.eachCount()
        // Blocks from accounts not read this run (failing, or only set up on another device) are left alone.
        val existing = events.filter { it.tag != null && it.tag in calendars }.groupBy({ it.key(it.tag!!) }, { it.id })
        Plan(
            create = wanted.flatMap { (key, n) -> List(maxOf(0, n - (existing[key]?.size ?: 0))) { key } },
            delete = existing.flatMap { (key, ids) -> ids.drop(wanted[key] ?: 0) },
        )
    }
}

class Syncer(private val store: AccountStore) {
    private fun graphFor(account: Account): Graph {
        if (account.expiresAt > System.currentTimeMillis()) return Graph(account.accessToken)
        val fresh = try {
            Auth.refresh(account)
        } catch (e: AuthException) {
            throw AuthException("${e.message} (remove and re-add this account)")
        }
        store.put(fresh)
        return Graph(fresh.accessToken)
    }

    fun sync(): List<String> {
        val today = LocalDate.now(ZoneOffset.UTC)
        val start = today.minusDays(1).atStartOfDay(ZoneOffset.UTC).toInstant().toString()
        val end = today.plusDays(BuildConfig.DAYS_AHEAD.toLong()).atStartOfDay(ZoneOffset.UTC).toInstant().toString()
        val accounts = store.all().associateBy { it.oid }
        val report = mutableListOf<String>()
        val graphs = linkedMapOf<String, Graph>()
        val calendars = linkedMapOf<String, List<Event>>()
        for (account in accounts.values) {
            try {
                val graph = graphFor(account)
                calendars[account.oid] = graph.events(start, end)
                graphs[account.oid] = graph
            } catch (e: Exception) {
                report += "${account.user}: ${e.message}"
            }
        }
        val sendOnly = accounts.values.filter { it.sendOnly }.map { it.oid }.toSet()
        for ((oid, p) in plan(calendars, BuildConfig.MIRROR_SHOW_AS.split(",").toSet(), sendOnly)) {
            val graph = graphs.getValue(oid)
            val failures = mutableListOf<String>()
            p.delete.forEach { id -> runCatching { graph.delete(id) }.onFailure { failures += "${it.message}" } }
            p.create.forEach { key -> runCatching { graph.createBlock(key) }.onFailure { failures += "${it.message}" } }
            val user = accounts.getValue(oid).user
            report += when {
                failures.isNotEmpty() -> "$user: ${failures.size} changes failed (${failures.first()})"
                p.create.isEmpty() && p.delete.isEmpty() -> "$user: up to date"
                else -> "$user: ${p.create.size} added, ${p.delete.size} removed"
            }
        }
        return report
    }

    fun remove(account: Account): List<String> {
        val now = Instant.now().truncatedTo(ChronoUnit.SECONDS)
        val start = now.minus(Duration.ofDays(360)).toString()
        val end = now.plus(Duration.ofDays(360)).toString()
        val problems = mutableListOf<String>()
        for (other in store.all()) {
            try {
                val graph = graphFor(other)
                graph.events(start, end)
                    .filter { it.tag != null && (other.oid == account.oid || it.tag == account.oid) }
                    .forEach { graph.delete(it.id) }
            } catch (e: Exception) {
                if (other.oid != account.oid) problems += "${other.user}: ${e.message}"
            }
        }
        if (problems.isEmpty()) store.remove(account.oid)
        return problems
    }
}
