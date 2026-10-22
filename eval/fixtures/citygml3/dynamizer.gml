<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel xmlns:core="http://www.opengis.net/citygml/3.0" xmlns:gml="http://www.opengis.net/gml/3.2"
    xmlns:frn="http://www.opengis.net/citygml/cityfurniture/3.0" xmlns:gen="http://www.opengis.net/citygml/generics/3.0"
    xmlns:dyn="http://www.opengis.net/citygml/dynamizer/3.0" xmlns:xlink="http://www.w3.org/1999/xlink" gml:id="dyn_fixture">
  <gml:name>Dynamizer fixture (pykci codec test T11)</gml:name>
  <gml:boundedBy><gml:Envelope srsName="urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH" srsDimension="3">
    <gml:lowerCorner>565000.0 5933000.0 5.0</gml:lowerCorner><gml:upperCorner>565010.0 5933010.0 8.0</gml:upperCorner>
  </gml:Envelope></gml:boundedBy>
  <core:cityObjectMember>
    <frn:CityFurniture gml:id="fx_charger">
      <gml:name>Ladesäule (Fixture)</gml:name>
      <core:genericAttribute><gen:GenericAttributeSet><gen:name>messwerte</gen:name>
        <gen:genericAttribute><gen:StringAttribute><gen:name>status_ladepunkt_01</gen:name><gen:value>AVAILABLE</gen:value></gen:StringAttribute></gen:genericAttribute>
        <gen:genericAttribute><gen:IntAttribute><gen:name>belegungen</gen:name><gen:value>3</gen:value></gen:IntAttribute></gen:genericAttribute>
      </gen:GenericAttributeSet></core:genericAttribute>
      <core:dynamizer>
        <dyn:Dynamizer gml:id="fx_charger_dyn_live">
          <gml:name>Status Ladepunkt 01</gml:name>
          <dyn:attributeRef>core:genericAttribute/gen:GenericAttributeSet[gen:name='messwerte']/gen:genericAttribute/gen:StringAttribute[gen:name='status_ladepunkt_01']/gen:value</dyn:attributeRef>
          <dyn:startTime>2024-07-18T13:12:06Z</dyn:startTime>
          <dyn:sensorConnection>
            <dyn:SensorConnection>
              <dyn:connectionType codeSpace="urn:x-pykci:codelist:dynamizer:connectionType">OGC SensorThings API</dyn:connectionType>
              <dyn:observationProperty>Status_E-Ladepunkt</dyn:observationProperty>
              <dyn:sensorName>Ladepunkt DE*HHM*E1011*01</dyn:sensorName>
              <dyn:datastreamID>18942</dyn:datastreamID>
              <dyn:baseURL>https://iot.hamburg.de/v1.1</dyn:baseURL>
              <dyn:mqttServer>iot.hamburg.de</dyn:mqttServer>
              <dyn:mqttTopic>v1.1/Datastreams(18942)/Observations</dyn:mqttTopic>
              <dyn:linkToObservation>https://iot.hamburg.de/v1.1/Datastreams(18942)/Observations</dyn:linkToObservation>
              <dyn:sensorLocation xlink:href="#fx_charger"/>
            </dyn:SensorConnection>
          </dyn:sensorConnection>
        </dyn:Dynamizer>
      </core:dynamizer>
      <core:dynamizer>
        <dyn:Dynamizer gml:id="fx_charger_dyn_ts">
          <dyn:attributeRef>core:genericAttribute/gen:GenericAttributeSet[gen:name='messwerte']/gen:genericAttribute/gen:IntAttribute[gen:name='belegungen']/gen:value</dyn:attributeRef>
          <dyn:startTime>2026-10-18T00:00:00Z</dyn:startTime>
          <dyn:endTime>2026-10-18T02:00:00Z</dyn:endTime>
          <dyn:dynamicData>
            <dyn:GenericTimeseries gml:id="fx_charger_ts">
              <dyn:firstTimestamp>2026-10-18T00:00:00Z</dyn:firstTimestamp>
              <dyn:lastTimestamp>2026-10-18T02:00:00Z</dyn:lastTimestamp>
              <dyn:observationProperty>belegungen</dyn:observationProperty>
              <dyn:uom>count</dyn:uom>
              <dyn:valueType>int</dyn:valueType>
              <dyn:timeValuePair><dyn:TimeValuePair><dyn:timestamp>2026-10-18T00:00:00Z</dyn:timestamp><dyn:intValue>1</dyn:intValue></dyn:TimeValuePair></dyn:timeValuePair>
              <dyn:timeValuePair><dyn:TimeValuePair><dyn:timestamp>2026-10-18T01:00:00Z</dyn:timestamp><dyn:intValue>2</dyn:intValue></dyn:TimeValuePair></dyn:timeValuePair>
              <dyn:timeValuePair><dyn:TimeValuePair><dyn:timestamp>2026-10-18T02:00:00Z</dyn:timestamp><dyn:intValue>3</dyn:intValue></dyn:TimeValuePair></dyn:timeValuePair>
            </dyn:GenericTimeseries>
          </dyn:dynamicData>
        </dyn:Dynamizer>
      </core:dynamizer>
      <core:lod1Solid><gml:Solid gml:id="fx_charger_solid"><gml:exterior><gml:Shell>
        <gml:surfaceMember><gml:Polygon gml:id="fx_p0"><gml:exterior><gml:LinearRing><gml:posList srsDimension="3">565000 5933000 5 565000 5933001 5 565001 5933001 5 565001 5933000 5 565000 5933000 5</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
        <gml:surfaceMember><gml:Polygon gml:id="fx_p1"><gml:exterior><gml:LinearRing><gml:posList srsDimension="3">565000 5933000 6.5 565001 5933000 6.5 565001 5933001 6.5 565000 5933001 6.5 565000 5933000 6.5</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
        <gml:surfaceMember><gml:Polygon gml:id="fx_p2"><gml:exterior><gml:LinearRing><gml:posList srsDimension="3">565000 5933000 5 565001 5933000 5 565001 5933000 6.5 565000 5933000 6.5 565000 5933000 5</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
        <gml:surfaceMember><gml:Polygon gml:id="fx_p3"><gml:exterior><gml:LinearRing><gml:posList srsDimension="3">565001 5933000 5 565001 5933001 5 565001 5933001 6.5 565001 5933000 6.5 565001 5933000 5</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
        <gml:surfaceMember><gml:Polygon gml:id="fx_p4"><gml:exterior><gml:LinearRing><gml:posList srsDimension="3">565001 5933001 5 565000 5933001 5 565000 5933001 6.5 565001 5933001 6.5 565001 5933001 5</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
        <gml:surfaceMember><gml:Polygon gml:id="fx_p5"><gml:exterior><gml:LinearRing><gml:posList srsDimension="3">565000 5933001 5 565000 5933000 5 565000 5933000 6.5 565000 5933001 6.5 565000 5933001 5</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
      </gml:Shell></gml:exterior></gml:Solid></core:lod1Solid>
      <frn:class codeSpace="https://www.sig3d.org/codelists/standard/cityfurniture/2.0/CityFurniture_class.xml">1000</frn:class>
      <frn:function codeSpace="https://www.sig3d.org/codelists/standard/cityfurniture/2.0/CityFurniture_function.xml">1540</frn:function>
    </frn:CityFurniture>
  </core:cityObjectMember>
</core:CityModel>
