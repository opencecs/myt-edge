#include "y4_os_cooling.h"
#include <errno.h>
static unsigned target(int64_t board)
{
    if(board<=35000)return 15;
    if(board>=65000)return 100;
    return 15u+(unsigned)((board-35000)*85/30000);
}
struct y4_cooling_output y4_cooling_step(struct y4_cooling *state,int read_error,
    const struct y4_temperature_snapshot *sample,int mapping_verified)
{
    struct y4_cooling_output out={.duty=100,.error=read_error};
    if(!state){out.error=-EINVAL;return out;}
    if(!out.error&&(!sample||!mapping_verified))out.error=-ENODATA;
    if(!out.error&&sample->scan_error)out.error=sample->scan_error;
    if(!out.error&&(!sample->pm_checked||sample->pm_error))
        out.error=sample->pm_error?sample->pm_error:-ENODATA;
    if(!out.error&&(!sample->soc_valid||sample->soc_error))
        out.error=sample->soc_error?sample->soc_error:-ENODATA;
    if(!out.error&&(sample->pm.millicelsius< -40000||sample->pm.millicelsius>125000||
        sample->soc_max< -40000||sample->soc_max>150000))out.error=-ERANGE;
    if(out.error){state->duty=100;state->valid_samples=state->lower_samples=0;return out;}
    out.valid=1;out.decicelsius=(int16_t)(sample->pm.millicelsius/100);
    if(state->valid_samples<3u)++state->valid_samples;
    if(state->duty<15u||state->duty>100u)state->duty=100;
    unsigned wanted=target(sample->pm.millicelsius);
    /* SoC is a separate safety override; it is never labelled board temperature. */
    unsigned soc_wanted=sample->soc_max<=55000?15u:
        sample->soc_max>=80000?100u:15u+(unsigned)((sample->soc_max-55000)*85/25000);
    if(soc_wanted>wanted)wanted=soc_wanted;
    if(state->valid_samples<3u)wanted=100;
    if(wanted>state->duty){state->duty=wanted;state->lower_samples=0;}
    else if(wanted<state->duty){
        if(++state->lower_samples>=5u){
            state->duty=state->duty-wanted>5u?state->duty-5u:wanted;
            state->lower_samples=0;
        }
    }else state->lower_samples=0;
    out.duty=state->duty;return out;
}
