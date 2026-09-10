package io.github.zanakis.calsync

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PlanTest {
    private val mirror = setOf("busy", "oof", "tentative")
    private var nextId = 0

    private fun ev(
        start: String,
        end: String,
        showAs: String = "busy",
        allDay: Boolean = false,
        cancelled: Boolean = false,
        tag: String? = null,
    ) = Event("e${nextId++}", tag, start, end, showAs, allDay, cancelled)

    private fun apply(cals: Map<String, MutableList<Event>>, plans: Map<String, Plan>) {
        for ((oid, p) in plans) {
            cals.getValue(oid).removeAll { it.id in p.delete }
            p.create.forEach { cals.getValue(oid) += ev(it.start, it.end, it.showAs, it.allDay, tag = it.source) }
        }
    }

    private fun blocks(events: List<Event>) = events.filter { it.tag != null }

    @Test
    fun mirrorsBusyTimeAndThenSettles() {
        val cals = mapOf(
            "A" to mutableListOf(
                ev("2026-09-10T09:00:00", "2026-09-10T10:00:00"),
                ev("2026-09-10T11:00:00", "2026-09-10T12:00:00", "tentative"),
                ev("2026-09-10T13:00:00", "2026-09-10T14:00:00", "free"),
                ev("2026-09-10T15:00:00", "2026-09-10T16:00:00", cancelled = true),
            ),
            "B" to mutableListOf(ev("2026-09-10T10:00:00", "2026-09-10T11:00:00")),
            "C" to mutableListOf(ev("2026-09-12T00:00:00", "2026-09-13T00:00:00", "oof", allDay = true)),
        )
        apply(cals, plan(cals, mirror))

        assertEquals(2, blocks(cals.getValue("A")).size)
        assertEquals(3, blocks(cals.getValue("B")).size)
        assertEquals(3, blocks(cals.getValue("C")).size)
        assertTrue(cals.getValue("A").any { it.tag == "C" && it.isAllDay && it.showAs == "oof" })
        assertTrue(blocks(cals.getValue("B")).none { it.tag == "B" })
        assertTrue(plan(cals, mirror).values.all { it.create.isEmpty() && it.delete.isEmpty() })
    }

    @Test
    fun replacesMovedEventsAndDropsDuplicates() {
        val cals = mapOf(
            "A" to mutableListOf(ev("2026-09-10T09:30:00", "2026-09-10T10:30:00")),
            "B" to mutableListOf(
                ev("2026-09-10T09:00:00", "2026-09-10T10:00:00", tag = "A"),
                ev("2026-09-10T09:30:00", "2026-09-10T10:30:00", tag = "A"),
                ev("2026-09-10T09:30:00", "2026-09-10T10:30:00", tag = "A"),
            ),
        )
        val p = plan(cals, mirror).getValue("B")
        assertEquals(0, p.create.size)
        assertEquals(2, p.delete.size)
        apply(cals, plan(cals, mirror))
        assertEquals(listOf("2026-09-10T09:30:00"), blocks(cals.getValue("B")).map { it.start })
    }

    @Test
    fun sendOnlyAccountGetsNoBlocksButStillSends() {
        val cals = mapOf(
            "A" to mutableListOf(ev("2026-09-10T09:00:00", "2026-09-10T10:00:00")),
            "P" to mutableListOf(
                ev("2026-09-10T17:00:00", "2026-09-10T18:00:00"),
                ev("2026-09-10T09:00:00", "2026-09-10T10:00:00", tag = "A"),
            ),
        )
        apply(cals, plan(cals, mirror, sendOnly = setOf("P")))
        assertTrue(blocks(cals.getValue("P")).isEmpty())
        assertEquals(listOf("P"), blocks(cals.getValue("A")).map { it.tag })
        assertTrue(plan(cals, mirror, sendOnly = setOf("P")).values.all { it.create.isEmpty() && it.delete.isEmpty() })
    }

    @Test
    fun leavesBlocksFromUnreadAccountsAlone() {
        val cals = mapOf(
            "A" to mutableListOf(ev("2026-09-11T08:00:00", "2026-09-11T09:00:00", tag = "Z")),
            "B" to mutableListOf<Event>(),
        )
        assertTrue(plan(cals, mirror).getValue("A").delete.isEmpty())
    }
}
