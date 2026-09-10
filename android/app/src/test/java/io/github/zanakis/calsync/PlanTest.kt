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
        uid: String? = null,
        subject: String? = null,
    ) = Event("e${nextId++}", tag, start, end, showAs, allDay, cancelled, uid, subject)

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
    fun sameMeetingInSeveralAccountsIsMirroredOnce() {
        val cals = mapOf(
            "A" to mutableListOf(ev("2026-09-10T10:00:00", "2026-09-10T11:00:00", "tentative", uid = "m1", subject = "Review")),
            "B" to mutableListOf(ev("2026-09-10T10:00:00", "2026-09-10T11:00:00", "busy", uid = "m1", subject = "Review")),
            "C" to mutableListOf<Event>(),
        )
        apply(cals, plan(cals, mirror))
        assertTrue(blocks(cals.getValue("A")).isEmpty())
        assertTrue(blocks(cals.getValue("B")).isEmpty())
        assertEquals(listOf("B" to "busy"), blocks(cals.getValue("C")).map { it.tag to it.showAs })
        assertTrue(plan(cals, mirror).values.all { it.create.isEmpty() && it.delete.isEmpty() })
    }

    @Test
    fun matchesSameMeetingBySubjectWhenIdsDiffer() {
        val cals = mapOf(
            "A" to mutableListOf(ev("2026-09-10T09:00:00", "2026-09-10T09:15:00", uid = "x", subject = "Standup")),
            "B" to mutableListOf(
                ev("2026-09-10T09:00:00", "2026-09-10T09:15:00", uid = "y", subject = "Standup"),
                ev("2026-09-10T09:00:00", "2026-09-10T09:15:00", uid = "z", subject = "Other"),
            ),
            "C" to mutableListOf<Event>(),
        )
        apply(cals, plan(cals, mirror))
        assertEquals(listOf("B"), blocks(cals.getValue("A")).map { it.tag })
        assertTrue(blocks(cals.getValue("B")).isEmpty())
        assertEquals(listOf("A", "B"), blocks(cals.getValue("C")).mapNotNull { it.tag }.sorted())
    }

    @Test
    fun removesBlocksLeftByEarlierDuplication() {
        val cals = mapOf(
            "A" to mutableListOf(
                ev("2026-09-10T10:00:00", "2026-09-10T11:00:00", "tentative", uid = "m1"),
                ev("2026-09-10T10:00:00", "2026-09-10T11:00:00", "tentative", tag = "B"),
            ),
            "B" to mutableListOf(
                ev("2026-09-10T10:00:00", "2026-09-10T11:00:00", "tentative", uid = "m1"),
                ev("2026-09-10T10:00:00", "2026-09-10T11:00:00", "tentative", tag = "A"),
            ),
            "C" to MutableList(4) { ev("2026-09-10T10:00:00", "2026-09-10T11:00:00", "tentative", tag = if (it < 2) "A" else "B") },
        )
        val p = plan(cals, mirror)
        assertEquals(listOf(1, 1, 3), listOf("A", "B", "C").map { p.getValue(it).delete.size })
        assertTrue(p.values.all { it.create.isEmpty() })
        apply(cals, p)
        assertEquals(listOf("A"), blocks(cals.getValue("C")).map { it.tag })
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
