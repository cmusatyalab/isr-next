   
import json
from django.http import HttpResponse

def info(request, id):
    data = {}
    # eventually get these values from the DB
    if id == '1':
        data['name'] = 'TurboTax'
        data['overview'] = ('TurboTax is an American tax preparation software '
            'package developed by Michael A. Chipman of Chipsoft in the '
            'mid-1980s. TurboTax became an Intuit product as a result of the '
            '1993 acquisition of its creator, San Diego-based Chipsoft. '
            'Chipsoft, now known as Intuit Consumer Tax Group, is still based '
            'in San Diego, having moved into a new office complex in 2007. '
            'Intuit Corporation is headquartered in Mountain View, '
            'California.')
        data['version'] = '2'
        data['size'] = '2 MB'
        data['status'] = 'Checked in'
        data['hoarded'] = 'No'

    elif id == '0':
        data['name'] = 'Oregon Trail 1.1 for Macintosh'
        data['overview'] = ('The Oregon Trail is a computer game originally '
            'developed by Don Rawitsch, Bill Heinemann, and Paul Dillenberger '
            'in 1971 and produced by the Minnesota Educational Computing '
            'Consortium (MECC) in 1974. The original game was designed to '
            'teach school children about the realities of 19th century pioneer '
            'life on the Oregon Trail. The player assumes the role of a wagon '
            'leader guiding his party of settlers from Independence, Missouri, '
            'to Oregon\'s Willamette Valley over the Oregon Trail via a '
            'covered wagon in 1848. The game has been released in many '
            'editions since the original release by various developers and '
            'publishers who have acquired rights to it.')
        data['version'] = '5'
        data['size'] = '3 GB'
        data['status'] = 'Checked out'
        data['hoarded'] = 'Yes'
   
    return HttpResponse(json.dumps(data), content_type='application/json')
