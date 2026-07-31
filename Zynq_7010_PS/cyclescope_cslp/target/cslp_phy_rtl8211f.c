/* Z7-Nano PS Ethernet PHY setup for a fixed 100BASE-TX full-duplex link. */

#include "netif/xemacpsif.h"


#define CYCLESCOPE_PHY_ADDRESS             1U

#define RTL8211F_BMCR                      0U
#define RTL8211F_BMSR                      1U
#define RTL8211F_PHY_ID1                   2U
#define RTL8211F_PHY_ID2                   3U
#define RTL8211F_ANAR                      4U
#define RTL8211F_GBCR                      9U
#define RTL8211F_PHYSR                     26U
#define RTL8211F_PAGE_SELECT               31U

#define RTL8211F_EXPECTED_ID1              0x001CU
#define RTL8211F_EXPECTED_ID2              0xC910U
#define RTL8211F_ID2_MASK                  0xFFF0U

#define RTL8211F_BMCR_RESET                0x8000U
#define RTL8211F_BMCR_LOOPBACK             0x4000U
#define RTL8211F_BMCR_SPEED_100            0x2000U
#define RTL8211F_BMCR_AUTONEG_ENABLE       0x1000U
#define RTL8211F_BMCR_POWER_DOWN           0x0800U
#define RTL8211F_BMCR_ISOLATE              0x0400U
#define RTL8211F_BMCR_AUTONEG_RESTART      0x0200U
#define RTL8211F_BMCR_FULL_DUPLEX          0x0100U
#define RTL8211F_BMCR_SPEED_1000           0x0040U

#define RTL8211F_BMSR_AUTONEG_COMPLETE     0x0020U
#define RTL8211F_BMSR_LINK_UP              0x0004U

#define RTL8211F_ANAR_10_100_MASK          0x01E0U
#define RTL8211F_ANAR_100_FULL             0x0100U
#define RTL8211F_ANAR_PAUSE                0x0400U
#define RTL8211F_ANAR_ASYM_PAUSE           0x0800U
#define RTL8211F_GBCR_ADVERTISE_MASK       0x0300U

#define RTL8211F_PHYSR_SPEED_MASK          0x0030U
#define RTL8211F_PHYSR_SPEED_100           0x0010U
#define RTL8211F_PHYSR_FULL_DUPLEX         0x0008U
#define RTL8211F_PHYSR_LINK_UP             0x0004U

#define RTL8211F_AUTONEG_POLLS             500U
#define RTL8211F_AUTONEG_POLL_US           10000U


u32_t phymapemac0[32];
u32_t phymapemac1[32];
static int rtl8211f_link_ready;


static int phy_read(XEmacPs *emac, u32_t reg, u16 *value)
{
    return XEmacPs_PhyRead(emac, CYCLESCOPE_PHY_ADDRESS, reg, value) ==
           XST_SUCCESS;
}


static int phy_write(XEmacPs *emac, u32_t reg, u16 value)
{
    return XEmacPs_PhyWrite(emac, CYCLESCOPE_PHY_ADDRESS, reg, value) ==
           XST_SUCCESS;
}


static int rtl8211f_read_identity(XEmacPs *emac, u16 *id1, u16 *id2)
{
    return phy_write(emac, RTL8211F_PAGE_SELECT, 0U) &&
           phy_read(emac, RTL8211F_PHY_ID1, id1) &&
           phy_read(emac, RTL8211F_PHY_ID2, id2);
}


static int rtl8211f_identity_matches(XEmacPs *emac)
{
    u16 id1;
    u16 id2;

    if (!rtl8211f_read_identity(emac, &id1, &id2)) {
        return 0;
    }
    return id1 == RTL8211F_EXPECTED_ID1 &&
           (id2 & RTL8211F_ID2_MASK) == RTL8211F_EXPECTED_ID2;
}

void detect_phy(XEmacPs *emac)
{
    u32_t index;
    u16 id1 = 0xFFFFU;
    u16 id2 = 0xFFFFU;

    rtl8211f_link_ready = 0;
    for (index = 0U; index < 32U; ++index) {
        phymapemac0[index] = FALSE;
        phymapemac1[index] = FALSE;
    }

    if (emac->Config.BaseAddress != XPAR_XEMACPS_0_BASEADDR) {
        xil_printf("CycleScope PHY: unsupported GEM base 0x%08lx\r\n",
                   (unsigned long)emac->Config.BaseAddress);
        return;
    }
    if (!rtl8211f_read_identity(emac, &id1, &id2) ||
        id1 != RTL8211F_EXPECTED_ID1 ||
        (id2 & RTL8211F_ID2_MASK) != RTL8211F_EXPECTED_ID2) {
        xil_printf("CycleScope PHY: RTL8211F not found at MDIO address 1 "
                   "(ID1=0x%04x ID2=0x%04x)\r\n",
                   (unsigned int)id1, (unsigned int)id2);
        return;
    }

    phymapemac0[CYCLESCOPE_PHY_ADDRESS] = TRUE;
    xil_printf("CYCLESCOPE_RTL8211F_ID_PASS ADDR=1 ID1=0x%04x "
               "ID2=0x%04x\r\n",
               (unsigned int)id1, (unsigned int)id2);
}


u32_t cyclescope_rtl8211f_force_100_full(XEmacPs *emac, u32_t phy_addr)
{
    u16 advertise;
    u16 gigabit_control;
    u16 control;
    u16 status = 0U;
    u16 phy_status = 0U;
    u32_t poll;

    rtl8211f_link_ready = 0;
    if (phy_addr != CYCLESCOPE_PHY_ADDRESS ||
        emac->Config.BaseAddress != XPAR_XEMACPS_0_BASEADDR ||
        !rtl8211f_identity_matches(emac)) {
        return XST_FAILURE;
    }

    /* Keep auto-negotiation enabled, but advertise only 100BASE-TX Full.
     * Forcing one side while the peer auto-negotiates would make the peer fall
     * back to half duplex.  The board straps already enable both RGMII delays.
     */
    if (!phy_read(emac, RTL8211F_ANAR, &advertise)) {
        return XST_FAILURE;
    }
    advertise &= (u16)~RTL8211F_ANAR_10_100_MASK;
    advertise |= RTL8211F_ANAR_100_FULL |
                 RTL8211F_ANAR_PAUSE |
                 RTL8211F_ANAR_ASYM_PAUSE;
    if (!phy_write(emac, RTL8211F_ANAR, advertise) ||
        !phy_read(emac, RTL8211F_GBCR, &gigabit_control)) {
        return XST_FAILURE;
    }
    gigabit_control &= (u16)~RTL8211F_GBCR_ADVERTISE_MASK;
    if (!phy_write(emac, RTL8211F_GBCR, gigabit_control) ||
        !phy_read(emac, RTL8211F_BMCR, &control)) {
        return XST_FAILURE;
    }

    control &= (u16)~(RTL8211F_BMCR_RESET |
                      RTL8211F_BMCR_LOOPBACK |
                      RTL8211F_BMCR_SPEED_100 |
                      RTL8211F_BMCR_POWER_DOWN |
                      RTL8211F_BMCR_ISOLATE |
                      RTL8211F_BMCR_AUTONEG_RESTART |
                      RTL8211F_BMCR_FULL_DUPLEX |
                      RTL8211F_BMCR_SPEED_1000);
    control |= RTL8211F_BMCR_SPEED_100 |
               RTL8211F_BMCR_AUTONEG_ENABLE |
               RTL8211F_BMCR_AUTONEG_RESTART |
               RTL8211F_BMCR_FULL_DUPLEX;
    if (!phy_write(emac, RTL8211F_BMCR, control)) {
        return XST_FAILURE;
    }

    for (poll = 0U; poll < RTL8211F_AUTONEG_POLLS; ++poll) {
        /* BMSR link is latch-low, so use the second read. */
        if (!phy_read(emac, RTL8211F_BMSR, &status) ||
            !phy_read(emac, RTL8211F_BMSR, &status)) {
            return XST_FAILURE;
        }
        if ((status & (RTL8211F_BMSR_AUTONEG_COMPLETE |
                       RTL8211F_BMSR_LINK_UP)) ==
            (RTL8211F_BMSR_AUTONEG_COMPLETE | RTL8211F_BMSR_LINK_UP)) {
            break;
        }
        usleep(RTL8211F_AUTONEG_POLL_US);
    }
    if (poll == RTL8211F_AUTONEG_POLLS ||
        !phy_read(emac, RTL8211F_PHYSR, &phy_status) ||
        (phy_status & RTL8211F_PHYSR_LINK_UP) == 0U ||
        (phy_status & RTL8211F_PHYSR_FULL_DUPLEX) == 0U ||
        (phy_status & RTL8211F_PHYSR_SPEED_MASK) !=
            RTL8211F_PHYSR_SPEED_100) {
        xil_printf("CycleScope PHY: 100BASE-TX Full negotiation failed "
                   "(BMSR=0x%04x PHYSR=0x%04x)\r\n",
                   (unsigned int)status, (unsigned int)phy_status);
        return XST_FAILURE;
    }

    xil_printf("CYCLESCOPE_RTL8211F_100_FULL_PASS PHYSR=0x%04x\r\n",
               (unsigned int)phy_status);
    rtl8211f_link_ready = 1;
    return 100U;
}


int cyclescope_rtl8211f_link_is_ready(void)
{
    return rtl8211f_link_ready;
}


u32_t phy_setup_emacps(XEmacPs *emac, u32_t phy_addr)
{
    return cyclescope_rtl8211f_force_100_full(emac, phy_addr);
}


int isphy_pcspma_external(XEmacPs *emac, u32_t phy_addr)
{
    (void)emac;
    (void)phy_addr;
    return 0;
}


void MacConfig_SgmiiPcs(XEmacPs *emac, u32_t phy_addr)
{
    (void)emac;
    (void)phy_addr;
}
